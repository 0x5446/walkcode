import os
from pathlib import Path
from dataclasses import dataclass

from .i18n import t


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


@dataclass
class Config:
    feishu_app_id: str
    feishu_app_secret: str
    feishu_receive_id: str  # may be empty during initial setup
    feishu_receive_id_type: str  # "open_id" or "chat_id"
    port: int = 3001
    state_path: Path = Path.home() / ".walkcode" / "state.json"
    default_cwd: str = str(Path.home() / ".walkcode" / "workspace")
    agent: str = "claude"
    instance: str = ""

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
            port=int(os.environ.get("WALKCODE_PORT", os.environ.get("PORT", "3001"))),
            state_path=Path(
                os.environ.get("WALKCODE_STATE_PATH", default_state)
            ).expanduser(),
            default_cwd=os.environ.get("WALKCODE_CWD", str(Path.home() / ".walkcode" / "workspace")),
            agent=agent,
            instance=instance,
        )
