"""Agent adapters for Claude Code and Codex CLI."""

import os
from dataclasses import dataclass


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

    def build_start_cmd(self, prompt: str, cwd: str, image_path: str | None = None) -> str:
        escaped = prompt.replace("'", "'\\''")
        parts = [f"cd '{cwd}'", "&&", self.command, self.permission_mode_flag]
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
        if self.resume_is_subcommand:
            # codex resume '<sid>'
            return f"cd '{cwd}' && {self.command} {self.resume_flag} '{escaped_sid}'"
        else:
            # claude --resume '<sid>' --permission-mode default
            return f"cd '{cwd}' && {self.command} {self.resume_flag} '{escaped_sid}' {self.permission_mode_flag}"

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
)

_AGENTS = {"claude": CLAUDE, "codex": CODEX}


def get_agent(name: str = "claude") -> AgentAdapter:
    if name not in _AGENTS:
        raise ValueError(f"Unknown agent: {name}. Must be one of: {', '.join(_AGENTS)}")
    return _AGENTS[name]
