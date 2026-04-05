"""Persistent session storage for Agent Hotline."""

import json
import logging
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("walkcode.state")


@dataclass
class Session:
    tty: str  # tmux session name
    cwd: str
    root_msg_id: str | None = None
    subscribed: bool = False  # user @mentioned for thread subscription
    created_at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        root_msg_id = data.get("root_msg_id")
        return cls(
            tty=str(data.get("tty", "")),
            cwd=str(data.get("cwd", "")),
            root_msg_id=str(root_msg_id) if root_msg_id else None,
            subscribed=bool(data.get("subscribed", False)),
            created_at=float(data.get("created_at", time.time())),
        )

    def to_dict(self) -> dict:
        return {
            "tty": self.tty,
            "cwd": self.cwd,
            "root_msg_id": self.root_msg_id,
            "subscribed": self.subscribed,
            "created_at": self.created_at,
        }


class SessionStore:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}
        self._root_to_session: dict[str, str] = {}
        # Pending Feishu-initiated sessions (not yet linked via hook)
        self._pending: dict[str, dict] = {}  # tmux_name → {"root_msg_id": str, "reply_id": str|None}
        self._pending_msg_to_tty: dict[str, str] = {}  # root_msg_id → tmux_name

    def load(self):
        with self._lock:
            self._sessions = {}
            self._pending = {}
            if self.path.exists():
                try:
                    payload = json.loads(self.path.read_text())
                except (OSError, json.JSONDecodeError) as e:
                    logger.warning("Failed to load state file %s: %s", self.path, e)
                    self._root_to_session = {}
                    self._pending_msg_to_tty = {}
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

                raw_pending = payload.get("pending", {})
                if isinstance(raw_pending, dict):
                    for tmux_name, entry in raw_pending.items():
                        if isinstance(entry, dict) and entry.get("root_msg_id"):
                            self._pending[str(tmux_name)] = {
                                "root_msg_id": str(entry["root_msg_id"]),
                                "reply_id": str(entry["reply_id"]) if entry.get("reply_id") else None,
                            }

            self._rebuild_index_locked()
            self._rebuild_pending_index_locked()
            self._save_locked()

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return None
            return Session(**session.to_dict())

    def items(self) -> list[tuple[str, Session]]:
        """Return a snapshot of all (session_id, Session) pairs."""
        with self._lock:
            return [(sid, Session(**s.to_dict())) for sid, s in self._sessions.items()]

    def resolve(self, *, root_id: str = "", parent_id: str = "") -> str | None:
        with self._lock:
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
    ) -> Session:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = Session(
                    tty=tty,
                    cwd=cwd,
                    root_msg_id=root_msg_id,
                )
                self._sessions[session_id] = session
            else:
                session.tty = tty
                session.cwd = cwd
                session.created_at = time.time()
                if root_msg_id is not None:
                    session.root_msg_id = root_msg_id

            self._sync_locked()
            return Session(**session.to_dict())

    def _sync_locked(self):
        self._rebuild_index_locked()
        self._save_locked()

    def _rebuild_index_locked(self):
        self._root_to_session = {
            session.root_msg_id: session_id
            for session_id, session in self._sessions.items()
            if session.root_msg_id
        }

    def _rebuild_pending_index_locked(self):
        self._pending_msg_to_tty = {
            entry["root_msg_id"]: tmux_name
            for tmux_name, entry in self._pending.items()
        }

    def _save_locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sessions": {
                session_id: session.to_dict()
                for session_id, session in self._sessions.items()
            },
            "pending": dict(self._pending),
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

    # --- Pending Feishu-initiated session helpers ---

    def add_pending(self, tmux_name: str, root_msg_id: str, reply_id: str | None = None):
        with self._lock:
            self._pending[tmux_name] = {"root_msg_id": root_msg_id, "reply_id": reply_id}
            self._pending_msg_to_tty[root_msg_id] = tmux_name
            self._save_locked()

    def update_pending_reply(self, tmux_name: str, reply_id: str):
        with self._lock:
            entry = self._pending.get(tmux_name)
            if entry:
                entry["reply_id"] = reply_id
                self._save_locked()

    def pop_pending(self, tmux_name: str) -> tuple[str | None, str | None]:
        with self._lock:
            entry = self._pending.pop(tmux_name, None)
            if not entry:
                return None, None
            self._pending_msg_to_tty.pop(entry["root_msg_id"], None)
            self._save_locked()
            return entry["root_msg_id"], entry.get("reply_id")

    def mark_subscribed(self, session_id: str):
        with self._lock:
            session = self._sessions.get(session_id)
            if session and not session.subscribed:
                session.subscribed = True
                self._save_locked()

    def resolve_pending_tty(self, msg_id: str) -> str | None:
        with self._lock:
            return self._pending_msg_to_tty.get(msg_id)
