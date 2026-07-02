# ADR 0035: Telegram Agent Markdown Rendering

Date: 2026-06-29

Status: Accepted

## Context

Claude Code and Codex commonly emit Markdown-style answers: headings,
`**bold**`, inline code, fenced code blocks, lists, and tables. Sending those
answers through Telegram `sendMessage` as plain text leaves the syntax visible
instead of rendering it.

Telegram now has two relevant Bot API surfaces:

- `sendMessage` / `editMessageText` with `parse_mode=HTML` or `MarkdownV2`;
- `sendRichMessage` with `InputRichMessage.markdown`, plus
  `editMessageText.rich_message`.

MarkdownV2 is not the same dialect as agent output. GitHub-style headings,
double-asterisk bold, and tables are not reliably rendered by simply setting
`parse_mode=MarkdownV2`.

## Decision

For Telegram agent output views (`turn_delta`, `turn_completed`, and explicit
text agent views), WalkCode detects common Markdown structures and uses a
layered delivery strategy:

1. By default, convert a conservative subset to Telegram HTML and send with
   `sendMessage(parse_mode=HTML)`.
2. If HTML parsing fails, fall back to the original plain text.
3. If delivery fails for a transient transport reason, such as Telegram 429
   flood limiting, do not fall back to another format in the same attempt; let
   the durable outbox retry the original delivery later.

`sendRichMessage` remains available only behind the explicit
`WALKCODE_TELEGRAM_RICH_MESSAGES=1` opt-in. When enabled, WalkCode tries
`sendRichMessage` first, then falls back to HTML and plain text.

For editable messages, the same strategy is used through
`editMessageText(parse_mode=HTML)`, then plain text when the final text fits in
one Telegram edit. The opt-in rich-message mode tries
`editMessageText.rich_message` first.

The conservative HTML fallback supports:

- Markdown headings as bold lines;
- `**bold**`, `__underline__`, `~~strike~~`;
- inline backtick code;
- fenced code blocks as `<pre>`;
- Markdown table rows as `<pre>` blocks;
- inline HTTP/HTTPS links.

Status cards and control cards are not sent as rich Markdown unless they carry
an agent-output view type.

## Consequences

- Stable Telegram clients receive compatible HTML-rendered messages by default.
- Rich messages are treated as an experimental compatibility-sensitive feature,
  because Bot API acceptance does not prove every currently stable Telegram
  client can render the new message type without showing an update notice.
- Rendering errors do not block message delivery.
- Transient delivery errors do block same-attempt fallback so the adapter does
  not accidentally send duplicate messages while Telegram is rate limiting.
- The adapter does not attempt to implement a full Markdown parser inside core
  V3; if richer fidelity is needed later, it should be isolated behind the same
  channel adapter boundary.

## Verification

Module tests cover:

- Markdown agent output is sent as `sendMessage(parse_mode=HTML)` by default;
- `sendRichMessage` is used only when explicitly enabled;
- rich-message or HTML failure falls back to the next compatible format;
- transient delivery errors do not fall back to another format;
- existing plain text and runtime delivery paths remain unchanged.
