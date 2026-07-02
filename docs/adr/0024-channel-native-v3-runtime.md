# ADR 0024: Channel-native V3 Runtime

Date: 2026-06-27

Status: Accepted

## Context

The channel-native V3 core is contract-tested and is the intended local product
path. Continuing to present legacy `walkcode serve/start/hook`, tmux wrappers,
or Feishu-only env files as the default path keeps old assumptions alive and
conflicts with the clean-slate runtime.

The first local deployment path should prove the clean-slate runtime without reusing the legacy Lark/tmux/hook server. Telegram is the first rollout channel. Lark remains a peer adapter type, but it must not be silently backed by the legacy Feishu runtime.

## Decision

Use the native runtime and CLI namespace as the V3 path:

- `walkcode native doctor` prints the selected channel, bound agent, and
  capability status.
- `walkcode native serve` runs the channel-native V3 runtime.
- `walkcode native serve --once` processes one polling cycle for local smoke tests and scripted verification.
- The V3 runtime builds its own `Orchestrator`, stores, selected channel adapter, and internal agent transports from `ChannelNativeConfig`.
- Telegram long polling is the first live ingress.
- Runtime state uses `JsonFileStateStore`.
- Top-level install/upgrade guidance must point to `walkcode native ...`.
- Top-level legacy commands such as `walkcode serve/start/hook/install-hooks`
  are hidden from help and rejected instead of writing legacy hooks or starting
  the old daemon path.
- `install.sh` and `upgrade.sh` are V3-only. They block on legacy LaunchAgent,
  `walkcode hook`, shell-wrapper, and `FEISHU_*` remnants instead of reporting
  them as non-fatal warnings.
- `walkcode upgrade` only upgrades the package, optionally restarts explicitly
  configured `WALKCODE_V3_LAUNCHD_LABELS`, and runs `walkcode native doctor`.
- Legacy `walkcode serve/start/hook` commands must not share a bot, webhook,
  state file, or hook config with V3.

One local runtime instance binds exactly one IM channel through
`WALKCODE_CHANNEL=telegram|lark`. Telegram and Lark are peer adapter types in
the project, but `WALKCODE_CHANNEL` is mandatory and old plural channel config
is rejected; users should run two instances with separate env files and state
paths if they need both ingress streams.

Agent binding is product-level config. `WALKCODE_AGENT=claude|codex` binds this
bot/app identity to one Coding Agent; low-level transport names such as
`claude_headless` and `codex_app_server` remain internal implementation details
and are not part of the recommended `.env` surface.

When `WALKCODE_STATE_PATH` is not set, the V3 runtime derives a non-shared
state path from the channel and agent: `~/.walkcode/{channel}-{agent}-state.json`.
Release docs still recommend an explicit state path in each runtime env file.

TUI observation uses `walkcode native hook`. IM-side takeover never kills an
external TUI process unless the hook payload explicitly includes
`allow_terminate=true`; a parent process inferred from the hook process is
read-only metadata and falls back to manual takeover.

Lark can be selected as a peer channel endpoint and represented in runtime status, but live Lark ingress for V3 remains behind a later channel-native runtime adapter. It must not be claimed as locally deployed until real V3 Lark ingress is wired and E2E-gated.

## Consequences

- Local users have one clear Telegram-first V3 command path.
- Old launchd plists, shell wrappers, hook configs, and `FEISHU_*` env files are
  migration cleanup items rather than supported V3 defaults.
- The V3 runtime creates a clean place for real Telegram/Lark/Claude/Codex E2E tests.

## Verification

Contract tests must prove:

- runtime assembly from `ChannelNativeConfig`;
- single-channel config through `WALKCODE_CHANNEL`;
- product-level agent config through `WALKCODE_AGENT`;
- rejection of removed plural/default transport config;
- rejection of legacy top-level CLI and hook installer paths;
- install/upgrade blocking when legacy remnants are present;
- Telegram update processing starts or reuses a session and persists state;
- Telegram polling tracks update offsets and dispatches inbound events;
- CLI `native doctor` and `native serve --once` call the V3 runtime without invoking the legacy server.

Verified with:

```text
uv run --with pytest python -m pytest \
  tests/test_channel_native_*.py \
  tests/test_release_scripts.py \
  tests/test_upgrade.py \
  tests/test_codex_hooks_feature.py

221 passed

bash -n install.sh && bash -n upgrade.sh
passed

uv run python -m compileall -q \
  src/walkcode/channel_native \
  src/walkcode/channel_native_runtime.py \
  src/walkcode/__main__.py \
  scripts/channel_native_debug.py
passed

env -i HOME=<tmp> TMPDIR=<tmp> PATH="$PATH" LANG="$LANG" ./upgrade.sh --dry-run
passed

uv run --with pytest python -m pytest
665 passed, 4 warnings

uv build
Successfully built dist/walkcode-0.10.54.tar.gz
Successfully built dist/walkcode-0.10.54-py3-none-any.whl
```
