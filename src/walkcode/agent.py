"""Agent adapters for Claude Code and Codex CLI."""

import os
import shlex
from dataclasses import dataclass


def _safe_flags(raw: str) -> str:
    """Split a user-supplied flag fragment and re-quote each token.

    WALKCODE_EXTRA_ARGS / WALKCODE_PERMISSION_FLAG come from the instance .env
    and get spliced into the `tmux new-session -d -s NAME "<cmd>"` string that a
    shell then executes. Re-quoting every token means a value can only ever be
    parsed as agent arguments, never as shell syntax (`;`, `$(...)`, backticks).
    Valid flags like `--settings /p/x.json` or `--yolo` are quote-invariant, so
    the emitted command is unchanged for normal config.
    """
    return " ".join(shlex.quote(tok) for tok in shlex.split(raw))


@dataclass(frozen=True)
class AgentAdapter:
    name: str
    command: str
    env_prefixes: tuple[str, ...]
    resume_flag: str                   # "--resume" (flag) or "resume" (subcommand)
    resume_is_subcommand: bool         # True if resume is a subcommand (codex resume <id>)
    permission_mode_flag: str
    extra_start_flags: tuple[str, ...]
    hook_event_permission: str         # "PermissionRequest" or "PreToolUse"
    settings_dir: str                  # ".claude" or ".codex"
    image_flag: str | None             # None = path injection, "--image" = native flag
    auth_error_patterns: tuple[str, ...]  # regex patterns to detect auth failure in tmux output
    device_auth_command: tuple[str, ...] | None  # command to run device-auth, None = not supported
    # PostToolUse hook: register it to invalidate stale Feishu permission cards once a
    # tool actually runs (the TUI already settled it). "PostToolUse" for claude; None
    # for codex (yolo emits no permission cards; a future non-yolo codex would set it).
    post_tool_hook_event: str | None
    # Future codex non-yolo dual-input: claude is parallel (Feishu via hook stdout +
    # TUI native menu, no injection); codex is serial, so its dual-input would need to
    # inject the Feishu decision back into the TUI. claude=False; codex placeholder.
    supports_inject_decision: bool
    # Env vars spliced inline right before the agent command in the tmux launch
    # string (`... && KEY=val agent ...`), so the agent process AND every hook it
    # spawns inherit them. codex sets WALKCODE_OWNER_CHECK=0: codex (>=0.140) runs
    # its hooks detached — the hook process is reparented to init, so walkcode's
    # pane-ownership gate (tty.owner_check, which walks the hook's process ancestry)
    # sees a severed chain and classifies every legitimate codex hook as a nested
    # sub-agent, dropping it (Feishu replies silently stop). The gate's ancestry
    # method cannot work for an agent that detaches its hooks, so codex opts out;
    # claude keeps the gate (synchronous hooks, intact ancestry → correctly owned).
    inline_env: tuple[tuple[str, str], ...] = ()

    def _command_with_inline_env(self) -> str:
        """The agent command, prefixed with any inline env assignments so the
        spawned agent (and the hooks it spawns) inherit them. See ``inline_env``."""
        if not self.inline_env:
            return self.command
        env_str = " ".join(f"{k}={shlex.quote(v)}" for k, v in self.inline_env)
        return f"{env_str} {self.command}"

    def build_start_cmd(self, prompt: str, cwd: str, image_path: str | None = None) -> str:
        escaped = prompt.replace("'", "'\\''")
        # Per-instance routing overrides, read from the instance env file
        # (loaded into os.environ by Config.load). Each walkcode instance is
        # single-agent, so these are unprefixed:
        #   WALKCODE_EXTRA_ARGS     extra flags inserted right after the agent
        #                           command, e.g. "--settings .../vertex.json"
        #                           to route Claude through Vertex (the `ccv` route).
        #   WALKCODE_PERMISSION_FLAG  replaces the default permission/approval
        #                           flag, e.g. "--yolo" for fully autonomous codex.
        perm_flag = os.environ.get("WALKCODE_PERMISSION_FLAG") or self.permission_mode_flag
        extra_args = os.environ.get("WALKCODE_EXTRA_ARGS", "").strip()
        parts = [f"cd '{cwd}'", "&&", self._command_with_inline_env()]
        if extra_args:
            parts.append(_safe_flags(extra_args))
        parts.append(_safe_flags(perm_flag))
        parts.extend(self.extra_start_flags)
        has_image = bool(image_path and self.image_flag)
        if has_image:
            parts.extend([self.image_flag, f"'{image_path}'"])
        # The positional prompt must NOT be appended when empty: codex's
        # `--image <FILE>...` is variadic, so a trailing '' is parsed as a second
        # (empty) image path and codex aborts at startup
        # ("a value is required for '--image <FILE>...' but none was supplied"),
        # killing the single-command tmux session. An image-only message has an
        # empty prompt, so this was every image message. When a prompt IS present
        # alongside an image, separate it from the variadic --image with `--`,
        # otherwise the prompt is swallowed as another image path.
        if prompt:
            if has_image:
                parts.append("--")
            parts.append(f"'{escaped}'")
        return " ".join(parts)

    def build_resume_cmd(self, session_id: str, cwd: str) -> str:
        escaped_sid = session_id.replace("'", "'\\''")
        # Same per-instance routing overrides as build_start_cmd, so a resumed
        # session keeps the same auth/approval routing as a fresh one.
        perm_override = os.environ.get("WALKCODE_PERMISSION_FLAG")
        extra_args = os.environ.get("WALKCODE_EXTRA_ARGS", "").strip()
        extra = f" {_safe_flags(extra_args)}" if extra_args else ""
        cmd = self._command_with_inline_env()
        if self.resume_is_subcommand:
            # codex resume '<sid>'. Global flags (e.g. --yolo) must precede the
            # `resume` subcommand: codex --yolo resume '<sid>'.
            perm = f" {_safe_flags(perm_override)}" if perm_override else ""
            return f"cd '{cwd}' && {cmd}{extra}{perm} {self.resume_flag} '{escaped_sid}'"
        else:
            # claude --settings <file> --resume '<sid>' --permission-mode default
            perm = _safe_flags(perm_override) if perm_override else self.permission_mode_flag
            return f"cd '{cwd}' && {cmd}{extra} {self.resume_flag} '{escaped_sid}' {perm}"

    def build_hook_response(
        self,
        behavior: str,
        updated_permissions: dict | None = None,
        updated_input: dict | None = None,
    ) -> dict | None:
        if self.hook_event_permission == "PreToolUse":
            # Codex PreToolUse protocol (from codex 0.124.0 binary strings):
            #   - `permissionDecision: "deny"` + non-empty `permissionDecisionReason` = block
            #   - `permissionDecision: "allow"` triggers a warning "unsupported
            #     permissionDecision:allow" in codex logs, but empirically codex
            #     still honors it as allow. Keep emitting it for now to avoid
            #     breaking the working allow path; revisit once we can
            #     empirically verify whether exit-0-no-output is safe.
            if behavior in ("allow", "always_allow", "accept_edits"):
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                    }
                }
            reason = f"Denied by user via Feishu (behavior={behavior})"
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        else:
            # Claude Code PermissionRequest: nested decision object, well-supported
            decision_obj = {"behavior": behavior}
            if updated_permissions:
                decision_obj["updatedPermissions"] = updated_permissions
            if updated_input is not None:
                # Used for AskUserQuestion to inject answers map directly,
                # bypassing the native TUI prompt entirely.
                decision_obj["updatedInput"] = updated_input
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": decision_obj,
                }
            }

    def hook_exit_code(self, behavior: str) -> int:
        """Exit code after printing the hook response (if any)."""
        if self.hook_event_permission == "PreToolUse":
            # Codex reads stdout JSON; exit code 0 so codex trusts printed JSON.
            return 0
        # Claude convention: 0 on allow (accept), 2 on deny (block with stderr).
        return 0 if behavior in ("allow", "always_allow", "accept_edits") else 2

    def build_env_exports(self) -> str:
        exports = []
        for key, value in os.environ.items():
            if any(key.startswith(p) for p in self.env_prefixes):
                escaped_val = value.replace("'", "'\\''")
                exports.append(f"export {key}='{escaped_val}'")
        return " && ".join(exports) + " && " if exports else ""


CLAUDE = AgentAdapter(
    name="claude",
    command="claude",
    env_prefixes=("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDE_"),
    resume_flag="--resume",
    resume_is_subcommand=False,
    permission_mode_flag="--permission-mode default",
    extra_start_flags=(),
    hook_event_permission="PermissionRequest",
    settings_dir=".claude",
    image_flag=None,
    auth_error_patterns=("Not logged in",),
    device_auth_command=None,
    post_tool_hook_event="PostToolUse",
    supports_inject_decision=False,
)

CODEX = AgentAdapter(
    name="codex",
    command="codex",
    env_prefixes=("OPENAI_", "CODEX_"),
    resume_flag="resume",
    resume_is_subcommand=True,
    permission_mode_flag="--ask-for-approval untrusted",
    extra_start_flags=("--no-alt-screen",),
    hook_event_permission="PreToolUse",
    settings_dir=".codex",
    image_flag="--image",
    auth_error_patterns=("refresh_token_expired", "Please log out and sign in", "unauthorized", "not authenticated"),
    device_auth_command=("codex", "login", "--device-auth"),
    post_tool_hook_event=None,
    supports_inject_decision=False,
    # codex detaches its hooks (reparented to init); walkcode's pane-ownership gate
    # would misclassify them as a nested sub-agent and drop every Feishu reply. Opt
    # codex out of the gate (claude keeps it). See the inline_env field docstring.
    inline_env=(("WALKCODE_OWNER_CHECK", "0"),),
)

_AGENTS = {"claude": CLAUDE, "codex": CODEX}


def get_agent(name: str = "claude") -> AgentAdapter:
    if name not in _AGENTS:
        raise ValueError(f"Unknown agent: {name}. Must be one of: {', '.join(_AGENTS)}")
    return _AGENTS[name]
