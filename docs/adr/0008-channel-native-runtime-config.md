# ADR 0008: Channel-native Runtime Configuration

Date: 2026-06-27

Status: Accepted

## Context

The legacy runtime config is Feishu-first: startup requires `FEISHU_APP_ID` and `FEISHU_APP_SECRET`. That conflicts with the V3 architecture where Telegram is the default rollout channel and Lark is a peer adapter, not a core identity model.

The new runtime also must avoid silently treating old `FEISHU_*` variables as active config. Silent compatibility would keep old assumptions alive and make clean-slate behavior ambiguous.

## Decision

Add a separate channel-native config model:

- one runtime instance selects exactly one IM channel through `WALKCODE_CHANNEL=telegram|lark` or infers one from `TELEGRAM_*` / `LARK_*` credentials;
- Telegram and Lark are peer adapter types in the codebase, but not simultaneous inbound channels in one local runtime instance;
- there is no `primary_channel`, because there is only one configured channel per instance;
- Lark is configured through `LARK_*` and is stored as the selected channel endpoint when `WALKCODE_CHANNEL=lark`;
- user-facing agent binding uses `WALKCODE_AGENT=claude|codex`; low-level transport names stay internal;
- runtime capability status represents only the agent bound to the current bot/app identity;
- `FEISHU_*` variables are ignored by runtime parsing;
- a one-shot conversion report can map known `FEISHU_*` keys into suggested `LARK_*` keys.

## Consequences

- New runtime startup does not depend on Feishu-named env vars.
- Telegram-first rollout becomes the default without making Lark second-class.
- Existing users get an auditable migration hint without runtime compatibility mode.

## Verification

Contract tests cover:

- Telegram and Lark single-channel config;
- rejection of multiple channels in one runtime instance;
- agent binding using product names;
- legacy Feishu env not activating a runtime channel;
- conversion report output.
