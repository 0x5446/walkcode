## Code Modification Preferences

- Ask for confirmation before writing code
- Propose a plan before directly modifying code
- Analyze existing code first before writing changes
- Don't create new files/components without explicit request

## After Implementation — End-to-End Verification

After completing code changes, you MUST perform thorough E2E verification in the real environment. Unit tests and local simulation alone are NOT sufficient.

### Verification methods (exhaust all applicable means)

- **Browser testing**: Use Playwright (or equivalent browser automation) to verify all UI flows, interactions, and visual states — do not rely on assumptions about rendering
- **Service logs**: Check application logs, container logs, and error outputs for warnings or failures
- **Container inspection**: Exec into running containers to inspect file systems, processes, environment variables, and runtime state
- **Network verification**: Inspect API calls, response codes, headers, and payloads; verify inter-service communication
- **Database validation**: Query databases directly to confirm data integrity, schema changes, and expected state transitions
- **CLI/Script validation**: Run relevant CLI commands, scripts, or health-check endpoints to confirm system behavior

### Mandatory rules

- Do NOT declare a task complete until all relevant verification methods above have been executed and passed
- If any verification fails, perform root-cause analysis — do not patch symptoms or move on
- Iterate the debug → fix → retest loop until all functionality is confirmed working end-to-end
- When in doubt about whether something works, verify it — never assume

## Release & Upgrade

Use the **`walkcode-release`** skill (`.claude/skills/walkcode-release/SKILL.md`) — it
encodes the gated, fully-automatable pipeline. Mechanical steps live in two root
scripts; orchestration and gates live in the skill.

- **Order is fixed: release first, then local upgrade** — `walkcode upgrade` pulls the
  latest GitHub *Release*, so the change must be merged to `main` and a Release created
  before upgrading locally.
- `./release.sh prepare [VERSION] -m MSG` — bump `pyproject.toml`, run tests, branch
  `release/vX.Y.Z`, commit, push, open PR. `./release.sh publish [VERSION]` — tag the
  merged `main` and `gh release create --latest`. Both support `--dry-run`.
- `./upgrade.sh` — `walkcode upgrade` + restart & verify **both** launchd instances
  (claude + codex).
- Hard rules: use the **0x5446** GitHub account; tests + `/deep-review` (no Critical)
  must pass before merging the PR; version's single source of truth is `pyproject.toml`
  (`__init__.py` derives from installed metadata — don't hand-edit it).
- `prepare` runs only on a clean `main` (== `origin/main`, no untracked files; `git add`
  new files first). **Don't share one checkout across parallel agents** — `git add -A`
  will sweep in another session's files; use a separate git worktree per task.
