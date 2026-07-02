# ADR 0036: Telegram Polling Transient Retry

Date: 2026-06-29

Status: Accepted

## Context

Local V3 deployments run Telegram bots as long-lived LaunchAgents. Telegram
long polling can occasionally fail with network timeouts or transient API
errors. Before this ADR, an uncaught `getUpdates` exception terminated
`walkcode native serve`; launchd restarted it, but users could see a short
period where messages were not handled.

## Decision

`serve_telegram_polling` treats Telegram polling exceptions as transient unless
they are local configuration errors:

- `ChannelConfigError` still fails fast;
- other exceptions are recorded in `last_telegram_poll_error`, logged to stderr,
  delayed by a small backoff, and then retried;
- a successful poll clears `last_telegram_poll_error`.

The lower-level `poll_telegram_once` keeps raising errors so module debug gates
and one-shot commands can still surface failures directly.

Telegram delivery also treats HTTP 429 and 5xx responses as transient delivery
failures:

- if Telegram returns `parameters.retry_after`, the durable outbox schedules the
  next delivery attempt no earlier than that value;
- Markdown rendering fallback does not retry the same payload in another format
  when the failure is transient, because that creates duplicate messages and
  worsens Telegram flood limiting;
- HTML/plain-text fallback is still used for actual formatting errors.

## Consequences

- Long-running Telegram LaunchAgents stay up across temporary network issues.
- One-shot diagnostics remain strict.
- Telegram flood limits are preserved as retryable outbox state instead of
  immediately dead-lettering or duplicating agent output.
- Polling retry behavior is testable without sleeping by passing
  `retry_delay=0` and a bounded `max_iterations` in tests.

## Verification

Module tests cover:

- polling retries after transient `getUpdates` failures;
- HTTP 429 maps to a transient delivery error carrying `retry_after`;
- durable outbox backoff honors `retry_after`;
- Markdown transient delivery failures do not fall back and duplicate a send.
