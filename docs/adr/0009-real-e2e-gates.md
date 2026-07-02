# ADR 0009: Real E2E Gates

Date: 2026-06-27

Status: Accepted

## Context

The V3 design depends on external systems: Telegram Bot API, Lark OpenAPI, Claude headless SDK, and Codex app-server. Unit and contract tests can verify boundaries, but they cannot prove the real services work in the current account, network, and credential environment.

At the same time, real E2E tests must not run accidentally. They can send messages, consume quotas, or depend on local auth state.

## Decision

Add an explicit E2E gate harness:

- every real E2E target has a named opt-in flag;
- credentials and target ids are declared as required environment variables;
- closed gates return a concrete skip reason and missing-variable list;
- tests may call the gate and skip rather than pretending success.

## Consequences

- The test suite can carry real E2E entry points without requiring secrets.
- CI and local runs remain deterministic by default.
- When a developer wants real verification, the required env is visible and checked before any external call.

## Verification

Contract tests cover:

- gates closed by default;
- missing variables reported after opt-in;
- gates enabled when all requirements are provided;
- test skip reasons are concrete.
