"""Channel-native configuration: env parsing, channel endpoints and E2E gates."""

from __future__ import annotations

import math
import os
import re
import sys
import urllib.parse

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import ChannelConfigError


# Codex app-server speaks two vocabularies for the same thing: requests take a
# SandboxMode string ("read-only"), responses echo a SandboxPolicy object
# ({"type": "readOnly"}). Keep the mapping in one place so the "did the server
# honour our override?" check can't drift from the value we send.
_CODEX_SANDBOX_POLICY_TYPES = {
    "read-only": "readOnly",
    "workspace-write": "workspaceWrite",
    "danger-full-access": "dangerFullAccess",
}


@dataclass(frozen=True)
class ChannelEndpointConfig:
    kind: str
    credentials: dict[str, str]
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelNativeConfig:
    channel: ChannelEndpointConfig
    agent: str
    agent_options: dict[str, dict[str, Any]]
    state_path: str
    cwd: str
    profile: str = ""
    workspace_roots: tuple[str, ...] = ()
    # ADR 0051: after a takeover-only handoff that stale-marked pending HITL
    # prompts, "auto" (default — user decision 2026-07-13) injects an
    # invisible continue turn so the agent re-asks and the channel gets a
    # fresh answerable card. "off" is the escape hatch.
    handoff_continue: str = "auto"
    # ADR 0053: ownership decisions (TUI handback / sentinel kill) only trust
    # hooks whose capture stamp is within this window. Parsed from the MERGED
    # env (WALKCODE_ENV_FILE included) — reading os.environ directly silently
    # ignored the value in launchd deployments (deep-review cluster E).
    tui_hook_fresh_seconds: float = 60.0
    # Kill switch for the post-takeover remnant sentinel. Disable to fall back
    # to notify-only if the sentinel ever misbehaves in production, without a
    # redeploy/rollback.
    tui_sentinel_enabled: bool = True

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ChannelNativeConfig":
        source = os.environ if env is None else env
        _reject_removed_runtime_env(source)
        _note_retired_runtime_env(source)
        channel_kind = _configured_channel_kind(source)
        if not channel_kind:
            raise ChannelConfigError(
                "no channel configured for channel-native runtime; "
                "set WALKCODE_ENV_FILE to bind this command to a runtime instance"
            )

        if channel_kind == "telegram":
            raise ChannelConfigError(
                "WALKCODE_CHANNEL=telegram is no longer supported: the Telegram channel "
                "was retired (ADR 0069). Use WALKCODE_CHANNEL=lark"
            )
        if channel_kind == "lark":
            channel = _lark_config_from_env(source)
        else:
            raise ChannelConfigError(f"unknown channel configured: {channel_kind}")

        agent = _configured_agent(source)
        supported_agent_names = ("claude", "codex")
        if agent not in supported_agent_names:
            raise ChannelConfigError(f"unknown agent configured: {agent}")

        profile = _configured_profile(source)
        handoff_continue = str(source.get("WALKCODE_HANDOFF_CONTINUE") or "").strip().lower()
        if handoff_continue and handoff_continue not in {"auto", "off"}:
            raise ChannelConfigError(
                f"invalid WALKCODE_HANDOFF_CONTINUE: {handoff_continue}; use auto or off"
            )
        fresh_raw = str(source.get("WALKCODE_TUI_HOOK_FRESH_SECONDS") or "").strip()
        tui_hook_fresh_seconds = 60.0
        if fresh_raw:
            try:
                parsed_fresh = float(fresh_raw)
            except ValueError:
                raise ChannelConfigError(
                    f"invalid WALKCODE_TUI_HOOK_FRESH_SECONDS: {fresh_raw}; must be a positive number"
                )
            # Reject inf/nan: `inf > 0` is True, which would make every stale
            # hook permanently "fresh" and disable the whole gate (round-2).
            if not math.isfinite(parsed_fresh) or parsed_fresh <= 0:
                raise ChannelConfigError(
                    f"invalid WALKCODE_TUI_HOOK_FRESH_SECONDS: {fresh_raw}; must be a finite positive number"
                )
            tui_hook_fresh_seconds = parsed_fresh
        sentinel_raw = str(source.get("WALKCODE_TUI_SENTINEL_ENABLED") or "").strip().lower()
        tui_sentinel_enabled = sentinel_raw not in {"0", "false", "off", "no"}
        return cls(
            channel=channel,
            agent=agent,
            agent_options=_configured_agent_options(source),
            state_path=str(
                Path(_configured_state_path(source, channel_kind, agent, profile)).expanduser()
            ),
            cwd=str(Path(source.get("WALKCODE_CWD", "~/.walkcode/workspace")).expanduser()),
            profile=profile,
            workspace_roots=tuple(
                str(Path(item).expanduser())
                for item in str(source.get("WALKCODE_WORKSPACE_ROOTS", "") or "").split(":")
                if item.strip()
            ),
            handoff_continue=handoff_continue or "auto",
            tui_hook_fresh_seconds=tui_hook_fresh_seconds,
            tui_sentinel_enabled=tui_sentinel_enabled,
        )

    @property
    def channel_kind(self) -> str:
        return self.channel.kind

    @property
    def agent_transport_kind(self) -> str:
        return _agent_to_transport_kind(self.agent)


@dataclass(frozen=True)
class E2EGateSpec:
    name: str
    flag: str
    required_env: tuple[str, ...]


@dataclass(frozen=True)
class E2EGateResult:
    name: str
    enabled: bool
    missing: tuple[str, ...] = ()
    reason: str = ""


class ChannelNativeE2EGates:
    _SPECS = {
        "lark": E2EGateSpec(
            name="lark",
            flag="WALKCODE_E2E_LARK",
            required_env=("LARK_APP_ID", "LARK_APP_SECRET", "WALKCODE_E2E_LARK_CHAT_ID"),
        ),
        "claude_headless": E2EGateSpec(
            name="claude_headless",
            flag="WALKCODE_E2E_CLAUDE_HEADLESS",
            required_env=("WALKCODE_E2E_CWD",),
        ),
        "codex_app_server": E2EGateSpec(
            name="codex_app_server",
            flag="WALKCODE_E2E_CODEX_APP_SERVER",
            required_env=("WALKCODE_E2E_CWD",),
        ),
    }

    def __init__(self, env: Any):
        self._env = env

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ChannelNativeE2EGates":
        return cls(os.environ if env is None else env)

    def evaluate(self, name: str) -> E2EGateResult:
        spec = self._SPECS.get(name)
        if spec is None:
            raise ValueError(f"unknown E2E gate: {name}")
        if not _env_bool(self._env.get(spec.flag), default=False):
            return E2EGateResult(
                name=name,
                enabled=False,
                reason=f"set {spec.flag}=1 to enable {name} E2E",
            )
        missing = tuple(key for key in spec.required_env if not self._env.get(key))
        if missing:
            return E2EGateResult(
                name=name,
                enabled=False,
                missing=missing,
                reason=f"missing required env for {name} E2E: {', '.join(missing)}",
            )
        return E2EGateResult(name=name, enabled=True)

    def all(self) -> dict[str, E2EGateResult]:
        return {name: self.evaluate(name) for name in self._SPECS}


def _split_csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _env_bool(raw: str | None, *, default: bool = False) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _configured_channel_kind(source: Any) -> str:
    explicit = str(source.get("WALKCODE_CHANNEL", "") or "").strip()
    if explicit:
        if "," in explicit:
            raise ChannelConfigError("WALKCODE_CHANNEL accepts exactly one channel: lark")
        return explicit
    return ""


def _configured_agent(source: Any) -> str:
    agent = str(source.get("WALKCODE_AGENT") or "").strip()
    if not agent:
        raise ChannelConfigError("missing WALKCODE_AGENT; set WALKCODE_AGENT=claude or WALKCODE_AGENT=codex")
    return _normalize_agent_name(agent)


def _configured_agent_options(source: Any) -> dict[str, dict[str, Any]]:
    claude: dict[str, Any] = {}
    settings = str(source.get("WALKCODE_CLAUDE_SETTINGS") or "").strip()
    if settings:
        claude["settings"] = str(Path(settings).expanduser())
    cli_path = str(source.get("WALKCODE_CLAUDE_CLI_PATH") or "").strip()
    if cli_path:
        claude["cli_path"] = str(Path(cli_path).expanduser())
    claude_config_dir = str(source.get("WALKCODE_CLAUDE_CONFIG_DIR") or "").strip()
    if claude_config_dir:
        claude["config_dir"] = str(Path(claude_config_dir).expanduser())
    claude_anthropic_base_url = str(source.get("WALKCODE_CLAUDE_ANTHROPIC_BASE_URL") or "").strip()
    if claude_anthropic_base_url:
        parsed_base_url = urllib.parse.urlsplit(claude_anthropic_base_url)
        if parsed_base_url.scheme not in {"http", "https"} or not parsed_base_url.hostname:
            raise ChannelConfigError(
                f"invalid WALKCODE_CLAUDE_ANTHROPIC_BASE_URL: {claude_anthropic_base_url}; "
                "must be a full http:// or https:// URL with a host"
            )
        if claude.get("settings"):
            # _option_kwargs() passes this override as a standalone --settings
            # payload rather than merging WALKCODE_CLAUDE_SETTINGS' own file
            # content into it (merging would mean re-serializing that file's
            # content — possibly including secrets like ANTHROPIC_API_KEY —
            # into this process' argv, and silently dropping it on any
            # read/parse failure). Combining the two is not supported.
            raise ChannelConfigError(
                "WALKCODE_CLAUDE_ANTHROPIC_BASE_URL cannot be combined with "
                "WALKCODE_CLAUDE_SETTINGS on the same profile; unset one of them"
            )
        claude["anthropic_base_url"] = claude_anthropic_base_url
    claude_permission_mode = str(source.get("WALKCODE_CLAUDE_PERMISSION_MODE") or "").strip()
    if claude_permission_mode:
        allowed_modes = {"default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"}
        if claude_permission_mode not in allowed_modes:
            raise ChannelConfigError(
                f"invalid WALKCODE_CLAUDE_PERMISSION_MODE: {claude_permission_mode}; "
                f"use one of {', '.join(sorted(allowed_modes))}"
            )
        claude["permission_mode"] = claude_permission_mode
    claude_settle_grace = str(source.get("WALKCODE_CLAUDE_SETTLE_GRACE") or "").strip()
    if claude_settle_grace:
        try:
            grace_value = float(claude_settle_grace)
        except ValueError:
            grace_value = -1.0
        if grace_value < 0:
            raise ChannelConfigError(
                f"invalid WALKCODE_CLAUDE_SETTLE_GRACE: {claude_settle_grace}; "
                "use a non-negative number of seconds"
            )
        claude["settle_grace_seconds"] = grace_value
    claude_bg_ceiling = str(source.get("WALKCODE_CLAUDE_BG_WAIT_CEILING") or "").strip()
    if claude_bg_ceiling:
        try:
            ceiling_value = float(claude_bg_ceiling)
        except ValueError:
            ceiling_value = -1.0
        if ceiling_value < 0:
            raise ChannelConfigError(
                f"invalid WALKCODE_CLAUDE_BG_WAIT_CEILING: {claude_bg_ceiling}; "
                "use a number of seconds (0 disables the ceiling)"
            )
        claude["background_wait_ceiling_seconds"] = ceiling_value
    claude_gate_mode = str(source.get("WALKCODE_CLAUDE_GATE_MODE") or "").strip().lower()
    if claude_gate_mode:
        if claude_gate_mode not in {"auto", "off", "ask_only"}:
            raise ChannelConfigError(
                f"invalid WALKCODE_CLAUDE_GATE_MODE: {claude_gate_mode}; use auto, off, or ask_only"
            )
        claude["gate_mode"] = claude_gate_mode
    claude_gate_timeout = str(source.get("WALKCODE_CLAUDE_GATE_TIMEOUT") or "").strip()
    if claude_gate_timeout:
        try:
            timeout_value = float(claude_gate_timeout)
        except ValueError:
            timeout_value = 0.0
        if timeout_value <= 0:
            raise ChannelConfigError(
                f"invalid WALKCODE_CLAUDE_GATE_TIMEOUT: {claude_gate_timeout}; use seconds > 0"
            )
        claude["gate_timeout"] = timeout_value
    claude_gate_tools = str(source.get("WALKCODE_CLAUDE_GATE_TOOLS") or "").strip()
    if claude_gate_tools:
        claude["gate_tools"] = [
            tool.strip() for tool in claude_gate_tools.split(",") if tool.strip()
        ]
    codex: dict[str, Any] = {}
    codex_home = str(source.get("WALKCODE_CODEX_HOME") or "").strip()
    if codex_home:
        codex["codex_home"] = str(Path(codex_home).expanduser())
    codex_config = str(source.get("WALKCODE_CODEX_CONFIG") or "").strip()
    if codex_config:
        codex["config"] = str(Path(codex_config).expanduser())
    codex_models_cache = str(source.get("WALKCODE_CODEX_MODELS_CACHE") or "").strip()
    if codex_models_cache:
        codex["models_cache"] = str(Path(codex_models_cache).expanduser())
    codex_app_server_mode = str(source.get("WALKCODE_CODEX_APP_SERVER_MODE") or "").strip()
    if codex_app_server_mode:
        codex["app_server_mode"] = codex_app_server_mode
    codex_app_server_socket = str(source.get("WALKCODE_CODEX_APP_SERVER_SOCKET") or "").strip()
    if codex_app_server_socket:
        codex["app_server_socket"] = str(Path(codex_app_server_socket).expanduser())
    codex_mirror = str(source.get("WALKCODE_CODEX_MIRROR") or "").strip().lower()
    if codex_mirror:
        if codex_mirror not in {"on", "off"}:
            raise ChannelConfigError(f"invalid WALKCODE_CODEX_MIRROR: {codex_mirror}; expected on or off")
        codex["mirror"] = codex_mirror
    codex_sandbox = str(source.get("WALKCODE_CODEX_SANDBOX") or "").strip()
    if codex_sandbox:
        # Mirrors the app-server protocol's SandboxMode enum (read-only /
        # workspace-write / danger-full-access). Setting this OVERRIDES the
        # codex profile's own `sandbox_mode`; leaving it unset means walkcode
        # sends no sandbox at all and the profile decides. It used to fall back
        # to read-only, which silently overrode profiles configured for
        # danger-full-access on every channel-launched thread.
        if codex_sandbox not in _CODEX_SANDBOX_POLICY_TYPES:
            raise ChannelConfigError(
                f"invalid WALKCODE_CODEX_SANDBOX: {codex_sandbox}; "
                "use one of read-only, workspace-write, danger-full-access"
            )
        codex["sandbox"] = codex_sandbox
    # Only write the key when opted in, matching every other option here: an
    # always-present key would make "codex has no configured options" untrue and
    # is indistinguishable from an explicit opt-out anyway.
    if _env_bool(source.get("WALKCODE_CODEX_ALLOW_UNRESTRICTED_WITHOUT_ALLOWLIST"), default=False):
        codex["unrestricted_without_allowlist_ok"] = True
    return {
        "claude": claude,
        "codex": codex,
    }


_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _configured_profile(source: Any) -> str:
    raw = str(source.get("WALKCODE_PROFILE") or "").strip()
    if not raw:
        return ""
    if not _PROFILE_RE.match(raw):
        raise ChannelConfigError(
            f"invalid WALKCODE_PROFILE: {raw!r}; use lowercase letters, digits, and dashes"
        )
    return raw


def _configured_state_path(source: Any, channel_kind: str, agent: str, profile: str = "") -> str:
    explicit = str(source.get("WALKCODE_STATE_PATH") or "").strip()
    if explicit:
        return explicit
    if profile:
        return f"~/.walkcode/{profile}-{agent}-state.json"
    return f"~/.walkcode/{channel_kind}-{agent}-state.json"


def _reject_removed_runtime_env(source: Any) -> None:
    removed = {
        "WALKCODE_CHANNELS": "use WALKCODE_CHANNEL=lark",
        "WALKCODE_PRIMARY_CHANNEL": "remove it; one runtime instance has exactly one WALKCODE_CHANNEL",
        "WALKCODE_TRANSPORTS": "remove it; AgentTransport wiring is internal",
        "WALKCODE_DEFAULT_TRANSPORT": "use WALKCODE_AGENT=claude|codex to bind this bot to one agent",
        "WALKCODE_DEFAULT_AGENT": "use WALKCODE_AGENT=claude|codex to bind this bot to one agent",
    }
    for key, guidance in removed.items():
        if str(source.get(key, "") or "").strip():
            raise ChannelConfigError(f"{key} is not supported by channel-native V3; {guidance}")


# Keys of the retired Claude daemon mode (ADR 0068). Deployed env files still
# carry them (e.g. WALKCODE_CLAUDE_SPAWN_MODE=headless), so unlike the keys
# above they must not stop the runtime: they are ignored with one notice.
RETIRED_RUNTIME_ENV_KEYS = (
    "WALKCODE_CLAUDE_DAEMON_MODE",
    "WALKCODE_CLAUDE_SPAWN_MODE",
    "WALKCODE_CLAUDE_LIST_ADOPT",
    "WALKCODE_CLAUDE_GATE_STYLE",
)
_retired_env_noticed = False


def _note_retired_runtime_env(source: Any) -> None:
    global _retired_env_noticed
    if _retired_env_noticed:
        return
    present = [key for key in RETIRED_RUNTIME_ENV_KEYS if str(source.get(key, "") or "").strip()]
    if not present:
        return
    _retired_env_noticed = True
    print(
        f"walkcode: ignoring retired env {','.join(present)} "
        "(Claude daemon mode was removed, ADR 0068); safe to delete",
        file=sys.stderr,
        flush=True,
    )


def _normalize_agent_name(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    if normalized in {"claude", "claude-code", "claude-headless"}:
        return "claude"
    if normalized in {"codex", "codex-app-server"}:
        return "codex"
    return value.strip()


def _agent_to_transport_kind(agent: str) -> str:
    normalized = _normalize_agent_name(agent)
    if normalized == "claude":
        return "claude_headless"
    if normalized == "codex":
        return "codex_app_server"
    raise ChannelConfigError(f"unknown agent configured: {agent}")


def _lark_config_from_env(source: Any) -> ChannelEndpointConfig:
    app_id = source.get("LARK_APP_ID", "")
    app_secret = source.get("LARK_APP_SECRET", "")
    missing = [key for key, value in (("LARK_APP_ID", app_id), ("LARK_APP_SECRET", app_secret)) if not value]
    if missing:
        raise ChannelConfigError(f"missing {', '.join(missing)} for lark channel")
    allowed_chat_ids = tuple(_split_csv(source.get("LARK_ALLOWED_CHAT_IDS", "")))
    if not allowed_chat_ids:
        e2e_chat_id = str(source.get("WALKCODE_E2E_LARK_CHAT_ID", "") or "").strip()
        if e2e_chat_id:
            allowed_chat_ids = (e2e_chat_id,)
    return ChannelEndpointConfig(
        kind="lark",
        credentials={"app_id": app_id, "app_secret": app_secret},
        options={
            "receive_id": source.get("LARK_RECEIVE_ID", ""),
            "receive_id_type": source.get("LARK_RECEIVE_ID_TYPE", "open_id"),
            "openapi_domain": source.get("LARK_OPENAPI_DOMAIN", "https://open.feishu.cn").rstrip("/"),
            "allowed_chat_ids": allowed_chat_ids,
            "allowed_open_ids": tuple(_split_csv(source.get("LARK_ALLOWED_OPEN_IDS", ""))),
            "tui_chat_id": str(source.get("WALKCODE_LARK_TUI_CHAT_ID", "") or "").strip(),
        },
    )


_CHANNEL_ENVIRONMENT_CONTEXT_TEMPLATE = """<environment_context>
Interaction context (important): The user is talking to you through {channel} chat via a relay. They are NOT at this machine and cannot see the local terminal, screen, browser windows, screenshots on disk, or any local file path. Your plain text replies are relayed to the chat automatically — never invoke chat/IM tools just to deliver a reply to the user. (Chat tools remain appropriate when the user explicitly asks you to message someone else or another channel.)
1. Never ask the user to look at, click, or scan anything on the local machine.
2. Anything the user must see or open — pages, files, previews, login flows — include it in your reply as a URL reachable from their phone/browser. Local paths like /Users/... or file:// are useless to them.
3. For QR/login flows: put the login URL in your reply instead of pointing at a QR code shown on this machine; refresh and re-share it if it expires.
4. Keep every reply self-contained — the user only sees what arrives in {channel}.
5. If the chat may include people other than the user: no credentials, no QR codes (or preview links of them), no login links carrying tokens. Share a token-free URL, or ask the user to continue in a private chat first.
</environment_context>"""

_CHANNEL_DISPLAY_NAMES = {
    "lark": "Feishu (Lark)",
}


def _channel_environment_context(channel_kind: str) -> str:
    channel = _CHANNEL_DISPLAY_NAMES.get(
        str(channel_kind or "").strip().lower(), "the remote chat"
    )
    return _CHANNEL_ENVIRONMENT_CONTEXT_TEMPLATE.format(channel=channel)


def _channel_allowlist_configured(channel: ChannelEndpointConfig) -> bool:
    """True when this channel restricts who may drive the agent.

    Every allowlist option is "empty means allow everyone" (see
    `_lark_chat_allowed` / `_lark_sender_allowed`), which is a reasonable
    bootstrap default on its own but not in combination with an unsandboxed
    agent. Any one list being non-empty counts as restricted: the channel
    applies chat-level and sender-level lists independently, so requiring both
    would reject setups that are already locked down by one of them.
    """
    options = channel.options or {}
    return any(
        bool(options.get(key))
        for key in ("allowed_chat_ids", "allowed_open_ids")
    )
