# ADR 0043: Work/Personal Profile Split

Date: 2026-07-02

Status: Accepted

## Context

WalkCode V3 runs on one Mac that hosts both work and personal coding. The
first deployable channel moves from Telegram to Feishu/Lark (ADR 0044), with
four runtime instances: {work, personal} x {claude, codex}. Work bots live in
the company Feishu tenant (open.feishu.cn); personal bots live in a Lark
tenant (open.larksuite.com).

Before this ADR the instance identity had only two dimensions
(channel, agent). Two profiles on the same channel+agent would collide on the
default state path, the launchd label was hard-coded to
`com.walkcode.telegram-{agent}`, and — the biggest gap — the agent
subprocesses inherited the runtime's environment with no way to pin
`CLAUDE_CONFIG_DIR` or `CODEX_HOME` per instance. `WALKCODE_ENV_FILE` values
never reach `os.environ`, so putting those variables in the env file silently
did nothing. There was also a silent fallback to
`~/.walkcode/telegram-claude.env` when `WALKCODE_ENV_FILE` was unset, which
with multiple instances would misroute TUI hooks to the wrong instance.

## Decision

- `WALKCODE_PROFILE` (lowercase `[a-z0-9-]+`) is a first-class config
  dimension. Empty profile keeps every legacy behavior unchanged.
- Derived naming with a profile:
  - state path `~/.walkcode/{profile}-{agent}-state.json`
    (explicit `WALKCODE_STATE_PATH` still wins);
  - launchd label `com.walkcode.{profile}-{agent}`;
  - conventions (docs, not code): env file `~/.walkcode/{profile}-{agent}.env`,
    logs `~/.walkcode/logs/{profile}-{agent}.*.log`.
- Claude isolation: `WALKCODE_CLAUDE_CONFIG_DIR` flows into
  `ClaudeAgentOptions(env={"CLAUDE_CONFIG_DIR": ...})`. The SDK merges
  `options.env` over the inherited environment, so each instance's Claude
  subprocesses see their profile's credentials, settings, and history without
  touching the runtime's own environment.
- Codex isolation: `WALKCODE_CODEX_HOME` is injected as `CODEX_HOME` into the
  codex stdio subprocess and the `codex app-server daemon start` spawn. Each
  profile therefore gets its own managed daemon and its own control socket at
  `{CODEX_HOME}/app-server-control/app-server-control.sock` (amends the single
  shared-daemon assumption of ADR 0041). The standalone-install probe, model
  inventory defaults, and hooks.json probe are all CODEX_HOME-aware.
- Unknown `WALKCODE_CODEX_APP_SERVER_MODE` values now fail fast instead of
  silently constructing a managed client without the configured socket path.
- TUI hook anchoring: the implicit `~/.walkcode/telegram-claude.env` fallback
  is removed. Every hook command must carry an explicit
  `WALKCODE_ENV_FILE=...` prefix. Because hook configs live inside each
  profile's `CLAUDE_CONFIG_DIR/settings.json` or `CODEX_HOME/hooks.json`, hook
  ownership follows the profile automatically once the commands are written
  with the profile's env file.

## Consequences

- Four instances coexist without sharing state, credentials, daemons, or hook
  spools: 4 bots, 4 env files, 4 launchd services, 4 state files, 2 codex
  daemons.
- `walkcode upgrade` restarts all instances listed in
  `WALKCODE_V3_LAUNCHD_LABELS`.
- A hook or CLI run without `WALKCODE_ENV_FILE` no longer lands on an
  arbitrary default instance; it fails with an actionable message.
- Cross-profile leakage of provider routing (work Vertex vs personal API key)
  is prevented at the process-environment level, not by convention.

## Verification

Contract tests prove: profile parsing and rejection of invalid names; state
path three-tier precedence; `config_dir`/`codex_home` flowing into agent
options, `ClaudeAgentOptions.env`, codex subprocess env, and the derived
socket path; both launchd label forms; doctor reporting the profile; and
`_load_native_env` having no implicit default env file.
