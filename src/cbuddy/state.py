"""Persistent session storage for CBuddy."""

import json
import logging
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("cbuddy.state")


@dataclass
class Session:
    tty: str
    cwd: str
    root_msg_id: str | None = None
    tty_pid: int | None = None
    tty_pid_started_at: str | None = None
    created_at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        root_msg_id = data.get("root_msg_id")
        tty_pid = data.get("tty_pid")
        tty_pid_started_at = data.get("tty_pid_started_at")
        parsed_tty_pid = int(tty_pid) if tty_pid not in (None, "") else None
        return cls(
            tty=str(data.get("tty", "")),
            cwd=str(data.get("cwd", "")),
            root_msg_id=str(root_msg_id) if root_msg_id else None,
            tty_pid=parsed_tty_pid,
            tty_pid_started_at=str(tty_pid_started_at) if tty_pid_started_at else None,
            created_at=float(data.get("created_at", time.time())),
        )

    def to_dict(self) -> dict:
        return {
            "tty": self.tty,
            "cwd": self.cwd,
            "root_msg_id": self.root_msg_id,
            "tty_pid": self.tty_pid,
            "tty_pid_started_at": self.tty_pid_started_at,
            "created_at": self.created_at,
        }


class SessionStore:
    def __init__(self, path: Path, ttl: int = 86400):
        self.path = Path(path).expanduser()
        self.ttl = ttl
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}
        self._root_to_session: dict[str, str] = {}

    def load(self):
        with self._lock:
            self._sessions = {}
            if self.path.exists():
                try:
                    payload = json.loads(self.path.read_text())
                except (OSError, json.JSONDecodeError) as e:
                    logger.warning("Failed to load state file %s: %s", self.path, e)
                    self._root_to_session = {}
                    return

                raw_sessions = payload.get("sessions", {})
                if isinstance(raw_sessions, dict):
                    for session_id, data in raw_sessions.items():
                        if not isinstance(data, dict):
                            continue
                        try:
                            self._sessions[str(session_id)] = Session.from_dict(data)
                        except (TypeError, ValueError) as e:
                            logger.warning("Skipping invalid session %s: %s", session_id, e)

            self._cleanup_locked()
            self._rebuild_index_locked()
            self._save_locked()

    def count(self) -> int:
        with self._lock:
            self._prune_locked()
            return len(self._sessions)

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            self._prune_locked()
            session = self._sessions.get(session_id)
            if not session:
                return None
            return Session(**session.to_dict())

    def resolve(self, *, root_id: str = "", parent_id: str = "") -> str | None:
        with self._lock:
            self._prune_locked()
            if root_id:
                return self._root_to_session.get(root_id)
            if parent_id:
                return self._root_to_session.get(parent_id)
            return None

    def touch(self, session_id: str) -> Session | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return None
            session.created_at = time.time()
            self._sync_locked()
            return Session(**session.to_dict())

    def upsert(
        self,
        session_id: str,
        tty: str,
        cwd: str,
        root_msg_id: str | None = None,
        tty_pid: int | None = None,
        tty_pid_started_at: str | None = None,
    ) -> Session:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = Session(
                    tty=tty,
                    cwd=cwd,
                    root_msg_id=root_msg_id,
                    tty_pid=tty_pid,
                    tty_pid_started_at=tty_pid_started_at,
                )
                self._sessions[session_id] = session
            else:
                session.tty = tty
                session.cwd = cwd
                session.created_at = time.time()
                if root_msg_id is not None:
                    session.root_msg_id = root_msg_id
                if tty_pid is not None:
                    session.tty_pid = tty_pid
                if tty_pid_started_at is not None:
                    session.tty_pid_started_at = tty_pid_started_at

            self._sync_locked()
            return Session(**session.to_dict())

    def _sync_locked(self):
        self._cleanup_locked()
        self._rebuild_index_locked()
        self._save_locked()

    def _prune_locked(self):
        if self._cleanup_locked():
            self._rebuild_index_locked()
            self._save_locked()

    def _cleanup_locked(self) -> bool:
        now = time.time()
        expired = [sid for sid, session in self._sessions.items() if now - session.created_at > self.ttl]
        for session_id in expired:
            self._sessions.pop(session_id, None)
        return bool(expired)

    def _rebuild_index_locked(self):
        self._root_to_session = {
            session.root_msg_id: session_id
            for session_id, session in self._sessions.items()
            if session.root_msg_id
        }

    def _save_locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sessions": {
                session_id: session.to_dict()
                for session_id, session in self._sessions.items()
            }
        }
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.path.parent,
            prefix=f"{self.path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(payload, tmp, ensure_ascii=False, indent=2)
            tmp.write("\n")
            tmp_path = Path(tmp.name)
        tmp_path.replace(self.path)
