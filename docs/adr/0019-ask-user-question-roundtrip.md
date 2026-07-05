# ADR 0019: AskUserQuestion Transport Round-trip

Date: 2026-06-27

Status: Accepted

## Context

AskUserQuestion already has a neutral state machine and channel rendering, but the loop is incomplete unless a transport-originated question can receive the final answer back at the transport boundary. Keeping answers only in `InteractionStore` would make the IM UI appear successful while the agent remains blocked.

## Decision

Add `ask_user.requested` as a first-class transport event. The Orchestrator registers an AskUserQuestion interaction with the transport request id and renders the neutral prompt.

Callbacks and Other text replies are authorized like user input. Before consuming a token or text answer, the Orchestrator checks `TransportCapabilities.ask_user_question`. Intermediate callbacks such as multi-select toggle or multi-question advancement update only the interaction state. Once the state machine produces final answers, the Orchestrator calls `AgentTransport.answer_user_question(...)`.

## Consequences

- AskUserQuestion becomes a real transport round-trip, not only a UI state machine.
- Reviewers cannot answer questions that would affect the agent turn.
- Unsupported transports fail with `capability_disabled` before consuming tokens.
- Codex app-server AskUserQuestion is enabled for app-server
  `item/tool/requestUserInput` and MCP elicitation server requests covered by
  ADR 0041/0042.

## Verification

Contract tests cover transport question prompt creation, single-select and multi-question final delivery, Other text delivery, role rejection, capability-disabled token preservation, and Claude headless delegation.
