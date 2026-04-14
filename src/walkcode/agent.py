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

    def build_start_cmd(self, prompt: str, cwd: str, image_path: str | None = None) -> str:
        escaped = prompt.replace("'", "'\\''")
        parts = [f"cd '{cwd}'", "&&", self.command, self.permission_mode_flag]
        parts.extend(self.extra_start_flags)
        if image_path and self.image_flag:
            parts.extend([self.image_flag, f"'{image_path}'"])
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

    def build_hook_response(self, behavior: str, updated_permissions: dict | None = None) -> dict:
        if self.hook_event_permission == "PreToolUse":
            # Codex format: flat permissionDecision
            codex_decision = "allow" if behavior in ("allow", "always_allow", "accept_edits") else "deny"
            output = {
                "hookEventName": "PreToolUse",
                "permissionDecision": codex_decision,
            }
        else:
            # Claude Code format: nested decision object
            decision_obj = {"behavior": behavior}
            if updated_permissions:
                decision_obj["updatedPermissions"] = updated_permissions
            output = {
                "hookEventName": "PermissionRequest",
                "decision": decision_obj,
            }
        return {"hookSpecificOutput": output}

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
)

_AGENTS = {"claude": CLAUDE, "codex": CODEX}


def get_agent(name: str = "claude") -> AgentAdapter:
    if name not in _AGENTS:
        raise ValueError(f"Unknown agent: {name}. Must be one of: {', '.join(_AGENTS)}")
    return _AGENTS[name]
