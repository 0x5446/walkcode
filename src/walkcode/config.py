import os
from pathlib import Path
from dataclasses import dataclass

from .i18n import t


DEFAULT_OPENAPI_DOMAIN = "https://open.feishu.cn"
DEFAULT_STUCK_THRESHOLD = 1800


def _load_env_file(env_file: Path):
    """Parse a .env file and set values into os.environ (won't overwrite existing)."""
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key and value:
            k = key.strip()
            if k not in os.environ:
                os.environ[k] = value.strip()


def parse_stuck_threshold(raw: str | None = None, default: int = DEFAULT_STUCK_THRESHOLD) -> int:
    """Return a positive watchdog threshold in seconds."""
    try:
        value = int(raw if raw is not None else os.environ.get("WALKCODE_STUCK_THRESHOLD", str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


@dataclass
class Config:
    feishu_app_id: str
    feishu_app_secret: str
    feishu_receive_id: str  # may be empty during initial setup
    feishu_receive_id_type: str  # "open_id" or "chat_id"
    openapi_domain: str = DEFAULT_OPENAPI_DOMAIN
    port: int = 3001
    state_path: Path = Path.home() / ".walkcode" / "state.json"
    default_cwd: str = str(Path.home() / ".walkcode" / "workspace")
    agent: str = "claude"
    instance: str = ""
    # --- session health card feature ---
    health_card_enabled: bool = True       # WALKCODE_HEALTH_CARD=0 kills the whole feature
    summary_vertex_project: str = ""
    summary_vertex_region: str = "global"
    summary_sa_path: str = ""
    summary_model: str = "claude-haiku-4-5"
    summary_timeout: float = 8.0
    stuck_threshold: int = DEFAULT_STUCK_THRESHOLD

    @property
    def summary_enabled(self) -> bool:
        """Title summarization self-disables unless a Vertex project + SA path are
        configured (so the feature is opt-in and never blocks startup)."""
        return bool(self.summary_vertex_project and self.summary_sa_path)

    @property
    def instance_name(self) -> str:
        return self.instance or self.agent

    @classmethod
    def env_file_path(cls) -> Path:
        override = os.environ.get("WALKCODE_ENV_FILE")
        if override:
            return Path(override).expanduser()
        return Path.home() / ".walkcode" / ".env"

    @classmethod
    def load(cls) -> "Config":
        """Load config from .env file and environment variables."""
        _load_env_file(cls.env_file_path())

        missing = []
        for key in ["FEISHU_APP_ID", "FEISHU_APP_SECRET"]:
            if not os.environ.get(key):
                missing.append(key)
        if missing:
            raise SystemExit(t("config.missing_vars", vars=", ".join(missing)))

        agent = os.environ.get("WALKCODE_AGENT", "claude")
        instance = os.environ.get("WALKCODE_INSTANCE", "")

        # summary is an optional aux feature: a malformed timeout must never block
        # startup, so degrade to the default instead of letting float() raise.
        try:
            summary_timeout = float(os.environ.get("WALKCODE_SUMMARY_TIMEOUT", "8") or "8")
        except ValueError:
            summary_timeout = 8.0

        # Compute state path with backward compat
        effective_instance = instance or agent
        if agent == "claude" and not instance:
            default_state = str(Path.home() / ".walkcode" / "state.json")
        else:
            default_state = str(Path.home() / ".walkcode" / f"{effective_instance}-state.json")

        return cls(
            feishu_app_id=os.environ["FEISHU_APP_ID"],
            feishu_app_secret=os.environ["FEISHU_APP_SECRET"],
            feishu_receive_id=os.environ.get("FEISHU_RECEIVE_ID", ""),
            feishu_receive_id_type=os.environ.get("FEISHU_RECEIVE_ID_TYPE", "open_id"),
            openapi_domain=os.environ.get(
                "LARK_OPENAPI_DOMAIN",
                os.environ.get("FEISHU_OPENAPI_DOMAIN", DEFAULT_OPENAPI_DOMAIN),
            ).rstrip("/"),
            port=int(os.environ.get("WALKCODE_PORT", os.environ.get("PORT", "3001"))),
            state_path=Path(
                os.environ.get("WALKCODE_STATE_PATH", default_state)
            ).expanduser(),
            default_cwd=os.environ.get("WALKCODE_CWD", str(Path.home() / ".walkcode" / "workspace")),
            agent=agent,
            instance=instance,
            health_card_enabled=os.environ.get("WALKCODE_HEALTH_CARD", "1") != "0",
            summary_vertex_project=os.environ.get("WALKCODE_SUMMARY_VERTEX_PROJECT", ""),
            summary_vertex_region=os.environ.get("WALKCODE_SUMMARY_VERTEX_REGION", "global"),
            summary_sa_path=os.environ.get("WALKCODE_SUMMARY_SA_PATH", ""),
            summary_model=os.environ.get("WALKCODE_SUMMARY_MODEL", "claude-haiku-4-5"),
            summary_timeout=summary_timeout,
            stuck_threshold=parse_stuck_threshold(),
        )
