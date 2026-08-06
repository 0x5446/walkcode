# ADR 0044: Lark Live Ingress and Card Rendering

Date: 2026-07-02

Status: Accepted

## Context

The user validated Telegram end to end and judged its interaction surface
(4096-char messages, inline keyboards only, markdown escaping) far worse than
Feishu cards for driving Claude Code / Codex. Decision: keep the V3
architecture, demote Telegram to an architecture-validation channel (code and
tests stay, no further UX work), and make Feishu/Lark the first deployable
channel — work on open.feishu.cn, personal on open.larksuite.com (ADR 0043).

`LarkChannelAdapter` and the orchestrator were already end-to-end tested
through the injected `LarkBotApi.call(method, payload)` seam, but there was no
HTTP layer, no runtime ingress, and no card rendering. The V2 Feishu UI
(permission three-button card, AskUserQuestion three modes, health card) is
battle-tested and is ported rather than reinvented
(`git show main:src/walkcode/server.py`).

## Decision

- **The fake seam does not move.** Everything live sits below
  `LarkBotApi.call`: the adapter and its existing tests are untouched.
- `channel_native/lark_cards.py`: pure renderers from V3 view models to Feishu
  `interactive` cards / `post` markdown. Ports V2's card layouts, `lark_md`
  escaping (prompt-injection defense), and truncation budgets (plan 800, tool
  input 500). Button values carry the V3 `{"token", "action"}` callback
  contract instead of V2's `{"rid", "b"}`. Unknown view types degrade to the
  pre-rendered text fallback, never to a delivery failure.
- `channel_native/lark_live.py`: `build_operation` (pure method→endpoint
  routing: reply-in-thread vs create, `im.v1.message.patch` for edits — the
  V2-proven no-cap card update path, `GetMessageResource` for downloads),
  `SdkTransport` on lark-oapi (lazy import; dual domain via
  `LARK_OPENAPI_DOMAIN`), transient/permanent error classification
  (permanent = content-caused codes such as 230001; retry/backoff stays in the
  OutboxDispatcher), `AckRegistry`, and `LarkIngressBridge`.
- **Ingress**: the lark-oapi WebSocket client runs in a daemon thread and
  forwards normalized events into an asyncio queue via
  `loop.call_soon_threadsafe`; callbacks return immediately (V2 lesson:
  blocking the SDK callback drops heartbeats and the reconnect redelivers).
  `serve_lark_ws` consumes the queue under the ingress lock and shares the
  outbox-flush / TUI-hook-drain / binding-refresh maintenance tasks with the
  Telegram loop. `serve --once` is rejected for lark (push has no pull
  semantics); `debug lark` covers preflight.
- **Callback ack within Feishu's ~3s window** (refines ADR 0013 for lark): the
  bridge parks a Future per card action; the orchestrator's `ack_callback`
  resolves it with a toast ("已收到"); on timeout the bridge answers a neutral
  toast. Button-state flips go exclusively through the durable outbox's
  `editCard` patch — the first version does not return inline raw cards, which
  avoids racing the patch path. Inline card flips are a later polish item.
- **Placement**: one session per reply chain. A non-reply message roots a new
  session; thread replies resolve through the existing binding. No
  topic-creation API is needed (contrast: ADR 0033's Telegram forum topics).
- **The root should be a health card, and that card is also the status card**
  (amended 2026-08-03; originally the chain rooted on the triggering message
  itself, and the TUI-observed path rooted on a plain text notice). Both
  placement paths now try to send a bot-owned health card and set
  `root_message_id == health_message_id`; the channel-initiated path forwards
  the user's original text into the thread as the first reply. Rationale: the
  collapsed thread list renders the root, so the root is the only surface a
  live session title can occupy — and Feishu's text-message edit API is capped
  (sender-only, 20 edits, admin-set window; error codes 230071/230072/230075)
  whereas card patches are uncapped.

  **This equality is best-effort, not an invariant**: if the root card cannot
  be sent, the channel-initiated path roots on the user's own message (no later
  heal), and the TUI-observed path may start rootless (the maintenance tick
  heals that one). Consumers must test the equality, never assume it. Two
  behaviours are conditioned on it:
  - when the status card IS the root, a failed edit does not fall back to
    sending a replacement card. Feishu has no "replace thread root" API, so the
    replacement lands as a child and the root stays frozen on its old title.
    Keep the pointer, log the degrade, retry on the next refresh — but bound
    the retry (`ROOT_CARD_EDIT_RETRY_BUDGET`; a `PermanentDeliveryError` skips
    the budget). A deleted or expired root fails identically forever, so on
    exhaustion the session demotes to a child status card rather than leaving
    the user with a card that never updates again.
  - the status-card dedup cache is keyed by `(message_id, fingerprint)`. A
    session can change status cards mid-life (rootless heal, demotion,
    send-fallback); a session-only key would match the old card's fingerprint
    and freeze the new card at whatever it was created with.
- **Session title on the root card** (2026-08-03): `Session.cached_title` is
  refreshed at turn end via one entry point and ranked by source
  (`tui_hook` < `turn_digest` < `initial_user_input` < `llm_summary`, reserved).
  Rank upgrades apply immediately; same-rank overwrites are throttled and only
  allowed for rolling sources, so the user's first prompt is not repainted by
  later ones. See ARCHITECTURE.md "Session titles" for the four feeding paths.
- **Message read-back** (added 2026-08-03): the adapter gains two read
  operations — `getMessage` (`GET /im/v1/messages/:id`) and
  `listThreadMessages` (`GET /im/v1/messages?container_id_type=thread`).
  Motivation: a `merge_forward` message's content is only
  `{"title", "message_id_list"}`, so a forwarded chat log reached the agent as
  its placeholder title; and a bot @-ed into an existing topic saw only the
  mention, never the discussion that prompted it. A forward read returns the
  container plus all children in one call, so no per-child fetch is needed.
  Thread history is seeded once, when the session for that thread is created.
  Requires `im:message` or `im:message:readonly` (group history also
  `im:message.group_msg`); on any failure the inbound degrades to the previous
  behaviour rather than dropping the turn. Rendering is bounded (per-message
  clip + total cap) with truncation stated inline.
- Ingress protection: `LARK_ALLOWED_CHAT_IDS` / `LARK_ALLOWED_OPEN_IDS`
  allowlists; `WALKCODE_E2E_LARK_CHAT_ID` restricts the runtime by default the
  same way the Telegram E2E chat id does. Redelivered WS events are absorbed
  by the InboundLedger (stable `lark:{event_id}` ids).
- Dependency: `lark-oapi` ships as the `lark` optional extra and via
  `walkcode upgrade`'s `--with lark-oapi`, keeping core
  `dependencies = []` (same pattern as `claude-agent-sdk`).

## Consequences

- One `LarkChannelAdapter` serves both tenants; the only difference between
  work and personal is credentials and `LARK_OPENAPI_DOMAIN`.
- All Lark route/render/ack logic is unit-testable without the SDK installed.
- Live evidence comes from `walkcode native debug lark` (SDK-free tenant token
  self-check) and `scripts/channel_native_debug.py lark --live`
  (send card → patch card against `WALKCODE_E2E_LARK_CHAT_ID`, gated by
  `WALKCODE_E2E_LARK`), extending the ADR 0009/0025 gate set.
- Telegram code paths remain intact and tested but receive no further UX
  investment.

## Verification

- `tests/test_channel_native_lark_cards.py`: per-view rendering, escaping,
  truncation.
- `tests/test_channel_native_lark_live.py`: operation routing, error
  classification, ack registry hit/timeout/replay, bridge thread→queue
  handoff and inline ack resolution.
- `tests/test_channel_native_lark.py`: runtime allowlists, command
  interception, reply-chain placement, serve loop consumption, doctor fields.
- Live gate: `channel_native_debug.py lark --live` per instance before
  promotion (ADR 0009).
