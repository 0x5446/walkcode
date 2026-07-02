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
  session at its own message id; thread replies resolve through the existing
  binding. No topic-creation API is needed (contrast: ADR 0033's Telegram
  forum topics).
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
