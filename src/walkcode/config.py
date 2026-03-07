import os
from pathlib import Path
from dataclasses import dataclass


@dataclass
class Config:
    feishu_app_id: str
    feishu_app_secret: str
    feishu_receive_id: str
    feishu_receive_id_type: str  # "open_id" or "chat_id"
    port: int = 3001
    state_path: Path = Path.home() / ".walkcode" / "state.json"
    default_cwd: str = str(Path.home())

    @classmethod
    def load(cls) -> "Config":
        """Load config from .env file and environment variables."""
        for env_file in [Path.cwd() / ".env", Path.home() / ".walkcode" / ".env"]:
            if env_file.exists():
                for line in env_file.read_text().splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    key, _, value = line.partition("=")
                    if key and value:
                        os.environ.setdefault(key.strip(), value.strip())

        missing = []
        for key in ["FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_RECEIVE_ID"]:
            if not os.environ.get(key):
                missing.append(key)
        if missing:
            raise SystemExit(f"Missing required env vars: {', '.join(missing)}\nSee .env.example")

        return cls(
            feishu_app_id=os.environ["FEISHU_APP_ID"],
            feishu_app_secret=os.environ["FEISHU_APP_SECRET"],
            feishu_receive_id=os.environ["FEISHU_RECEIVE_ID"],
            feishu_receive_id_type=os.environ.get("FEISHU_RECEIVE_ID_TYPE", "open_id"),
            port=int(os.environ.get("PORT", "3001")),
            state_path=Path(
                os.environ.get(
                    "WALKCODE_STATE_PATH",
                    str(Path.home() / ".walkcode" / "state.json"),
                )
            ).expanduser(),
            default_cwd=os.environ.get("WALKCODE_CWD", str(Path.home())),
        )
