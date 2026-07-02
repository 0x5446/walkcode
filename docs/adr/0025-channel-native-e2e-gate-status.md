# ADR 0025: Channel-native E2E Gate Status

Date: 2026-06-27

Status: Accepted

## Context

The V3 runtime can be built and started locally, but real Telegram, Lark,
Claude headless, and Codex app-server E2E runs intentionally remain behind
explicit environment gates. Without a CLI surface, users must inspect tests or
source code to understand why those checks are not running.

## Decision

Expose the E2E gate state in `walkcode native doctor` and
`walkcode native doctor --json`.

The status must include only safe metadata:

- gate name;
- enabled boolean;
- missing environment variable names;
- actionable reason.

It must not print credential values.

## Consequences

- Local deployment checks can distinguish "V3 runtime is configured" from
  "external E2E evidence is available".
- Release notes can point users to one command for capability and gate status.
- The V3 runtime still does not run external E2E checks unless the explicit
  `WALKCODE_E2E_*` gates are enabled.

## Verification

Contract tests must prove:

- JSON doctor output includes gate status without secret values.
- Text doctor output lists gate status in a compact human-readable form.

Verified with:

```text
uv run --with pytest python -m pytest tests/test_channel_native_runtime.py tests/test_channel_native_cli.py
10 passed

uv run --with pytest python -m pytest tests/test_channel_native_*.py
128 passed

uv run --with pytest python -m pytest
603 passed, 4 warnings
```
