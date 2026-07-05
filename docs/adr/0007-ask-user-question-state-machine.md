# ADR 0007: AskUserQuestion State Machine

Date: 2026-06-27

Status: Accepted

## Context

AskUserQuestion is not just a renderable card. Real IM use needs several steps:

- one agent request can contain multiple questions;
- some questions are single-select;
- some are multi-select and need a submit action;
- users can choose `Other`, then type a free-form answer in the same conversation;
- Telegram and Lark should render this differently, but the core state must stay platform-neutral.

## Decision

Model AskUserQuestion as an `InteractionStore` state machine:

- option callbacks update answers and either advance to the next question or finalize the interaction;
- multi-select callbacks toggle selected options and only finalize on submit;
- `Other` callbacks bind an awaiting-text state to `ChannelBinding.key()`;
- inbound text on that binding completes the waiting answer before normal turn submission;
- final decisions are write-once and include all answers.

## Consequences

- Telegram and Lark can keep different UI/UX while sharing one interaction contract.
- Free-form answers no longer depend on Lark thread-specific lookup.
- The orchestrator has a clear precedence rule: pending interaction text is handled before agent turn input.

## Verification

Contract tests cover:

- multi-question single-select progression;
- multi-select toggle and submit;
- callback-driven `Other` awaiting state;
- orchestrator callback and text routing for `Other`.
