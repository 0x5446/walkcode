"""Persistent session storage for Agent Hotline."""

import json
import logging
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("walkcode.state")

# Cap stashed-but-undelivered replies per session so a long Feishu outage can't
# grow state.json without bound. Oldest entries are dropped past this.
_MAX_REDELIVERY = 20


@dataclass
class Session:
    tty: str  # tmux session name
    cwd: str
    root_msg_id: str | None = None
    subscribed: bool = False  # user @mentioned for thread subscription
    created_at: float = field(default_factory=time.time)
    # Replies whose Feishu delivery failed (e.g. a DNS/connection blip): stashed
    # here and re-sent on the session's next hook, so a transient outage drops no
    # agent output (the silent-drop bug behind "answered in tmux but never on
    # Feishu"). Each entry is {"key": <dedupe-key list>|None, "text": str}; the key
    # lets redelivery register dedupe so codex's duplicate Stop for the same turn
    # isn't sent twice.
    pending_redelivery: list[dict] = field(default_factory=list)
    # --- health card (session health card feature) ---
    health_card_id: str = ""        # feishu interactive card message id (the thread root)
    cached_title: str = ""          # AI summary title (when summarizer refines it); else unset
    title_source: str = ""          # "" | "summary" (summary = haiku-refined, rate-limit gate)
    status: str = "running"         # "running" | "stopped"
    stop_reason: str = ""           # "" | completed | permission_request | ask_user_question | interrupted | agent_error | agent_exited | unknown
    interrupt_reason: str = ""      # "" | timeout | user
    running_since: float = 0.0      # epoch seconds for the current timeout timer t0

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        root_msg_id = data.get("root_msg_id")
        pending: list[dict] = []
        raw_pending = data.get("pending_redelivery")
        if isinstance(raw_pending, list):
            for item in raw_pending:
                if isinstance(item, dict) and item.get("text") is not None:
                    key = item.get("key")
                    pending.append({
                        "key": [str(k) for k in key] if isinstance(key, list) else None,
                        "text": str(item["text"]),
                    })
                elif isinstance(item, str):  # legacy list[str] form
                    pending.append({"key": None, "text": item})
        status = str(data.get("status", "") or "")
        stop_reason = str(data.get("stop_reason", "") or "")
        interrupt_reason = str(data.get("interrupt_reason", "") or "")
        if not status:
            legacy = str(data.get("last_status", "") or "")
            if legacy == "timeout":
                status = "stopped"
                stop_reason = "interrupted"
                interrupt_reason = interrupt_reason or "timeout"
            elif legacy == "stopped":
                status = "stopped"
                stop_reason = stop_reason or "completed"
            else:
                status = "running"
        if status not in ("running", "stopped"):
            status = "running"
            stop_reason = ""
            interrupt_reason = ""
        if status == "running":
            stop_reason = ""
            interrupt_reason = ""
        elif not stop_reason:
            stop_reason = "unknown"

        return cls(
            tty=str(data.get("tty", "")),
            cwd=str(data.get("cwd", "")),
            root_msg_id=str(root_msg_id) if root_msg_id else None,
            subscribed=bool(data.get("subscribed", False)),
            created_at=float(data.get("created_at", time.time())),
            pending_redelivery=pending,
            health_card_id=str(data.get("health_card_id", "")),
            cached_title=str(data.get("cached_title", "")),
            title_source=str(data.get("title_source", "")),
            status=status,
            stop_reason=stop_reason,
            interrupt_reason=interrupt_reason,
            running_since=float(data.get("running_since", 0.0) or 0.0),
        )

    def to_dict(self) -> dict:
        return {
            "tty": self.tty,
            "cwd": self.cwd,
            "root_msg_id": self.root_msg_id,
            "subscribed": self.subscribed,
            "created_at": self.created_at,
            "pending_redelivery": [
                {"key": list(x["key"]) if x.get("key") else None, "text": x["text"]}
                for x in self.pending_redelivery
            ],
            "health_card_id": self.health_card_id,
            "cached_title": self.cached_title,
            "title_source": self.title_source,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "interrupt_reason": self.interrupt_reason,
            "running_since": self.running_since,
        }

    @property
    def last_status(self) -> str:
        """Legacy in-memory view. New state is status/stop_reason."""
        if self.status == "running":
            return ""
        if self.stop_reason == "interrupted" and self.interrupt_reason == "timeout":
            return "timeout"
        return "stopped"

    @last_status.setter
    def last_status(self, value: str) -> None:
        if value == "timeout":
            self.status = "stopped"
            self.stop_reason = "interrupted"
            self.interrupt_reason = "timeout"
            self.running_since = 0.0
        elif value == "stopped":
            self.status = "stopped"
            self.stop_reason = "completed"
            self.interrupt_reason = ""
            self.running_since = 0.0
        else:
            self.status = "running"
            self.stop_reason = ""
            self.interrupt_reason = ""


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
                                "cwd": str(entry["cwd"]) if entry.get("cwd") else "",
                                "health_card_id": str(entry["health_card_id"]) if entry.get("health_card_id") else "",
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
        *,
        can_evict: Callable[[str, "Session"], bool] | None = None,
        cwd_is_launch: bool = False,
    ) -> Session:
        """Create or update a session's tty/cwd mapping.

        ``can_evict`` guards tty takeover. When another session already owns
        ``tty``, it is called as ``can_evict(other_id, other_session)``; returning
        False refuses the takeover — the incoming session does NOT claim ``tty``
        (its tty is cleared) and the existing owner keeps its mapping. This stops
        a nested child agent — one that merely inherited the parent's ``$TMUX`` and
        fires its own SessionStart/Stop hooks — from displacing a live,
        Feishu-bound parent session and orphaning its thread. Default (None) keeps
        the legacy "last writer wins" behavior (used by resume, where the new tmux
        name never collides).
        """
        with self._lock:
            if tty:
                for other_id, other in self._sessions.items():
                    if other_id != session_id and other.tty == tty:
                        if can_evict is not None and not can_evict(other_id, other):
                            logger.info(
                                "Refusing tty takeover: session=%s keeps tty=%s "
                                "(claimed by session=%s, likely nested child)",
                                other_id[:8], tty, session_id[:8],
                            )
                            tty = ""  # incoming session must not claim this tty
                            break
                        logger.info(
                            "Evicting stale tty mapping: session=%s tty=%s (taken over by session=%s)",
                            other_id[:8], tty, session_id[:8],
                        )
                        other.tty = ""

            session = self._sessions.get(session_id)
            if session is None:
                # cwd is the session's *launch* dir — where the agent rollout file
                # lives, the dir `--resume` must cd into. Only a trusted launch
                # source (SessionStart sync / resume / Feishu pending) may set it.
                # A runtime hook (Stop/Notification/Permission) that creates the
                # record first — e.g. the best-effort SessionStart upload was
                # dropped — reports a possibly-drifted cwd, so it leaves cwd empty
                # rather than locking the wrong dir; resume then falls back to
                # config.default_cwd, and a later trusted sync can still fill it.
                session = Session(
                    tty=tty,
                    cwd=cwd if cwd_is_launch else "",
                    root_msg_id=root_msg_id,
                )
                self._sessions[session_id] = session
            else:
                session.tty = tty
                # Never let a runtime hook's drifted cwd overwrite the launch cwd,
                # nor establish one; a later trusted sync still can while the field
                # is empty. Mirrors the root_msg_id guard below.
                if cwd and cwd_is_launch and not session.cwd:
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

    def add_pending(self, tmux_name: str, root_msg_id: str, reply_id: str | None = None,
                    cwd: str = "", health_card_id: str = "") -> str | None:
        with self._lock:
            for session_id, session in self._sessions.items():
                if session.tty == tmux_name and not session.root_msg_id:
                    session.root_msg_id = root_msg_id
                    if cwd and not session.cwd:
                        session.cwd = cwd
                    if health_card_id:
                        session.health_card_id = health_card_id
                    self._sync_locked()
                    return session_id
            # cwd is the agent's launch dir (config.default_cwd for Feishu-initiated
            # starts). Carried so that if SessionStart sync is dropped, the first
            # runtime hook can still establish the correct launch cwd from here.
            # health_card_id: the bot card created as the thread root (Feishu-
            # initiated), migrated onto the Session by the first hook.
            self._pending[tmux_name] = {"root_msg_id": root_msg_id, "reply_id": reply_id,
                                        "cwd": cwd, "health_card_id": health_card_id}
            self._pending_msg_to_tty[root_msg_id] = tmux_name
            self._save_locked()
            return None

    def update_pending_reply(self, tmux_name: str, reply_id: str):
        with self._lock:
            entry = self._pending.get(tmux_name)
            if entry:
                entry["reply_id"] = reply_id
                self._save_locked()

    def pop_pending(self, tmux_name: str) -> tuple[str | None, str | None, str | None, str | None]:
        with self._lock:
            entry = self._pending.pop(tmux_name, None)
            if not entry:
                return None, None, None, None
            self._pending_msg_to_tty.pop(entry["root_msg_id"], None)
            self._save_locked()
            return (entry["root_msg_id"], entry.get("reply_id"), entry.get("cwd"),
                    entry.get("health_card_id"))

    def mark_subscribed(self, session_id: str):
        with self._lock:
            session = self._sessions.get(session_id)
            if session and not session.subscribed:
                session.subscribed = True
                self._save_locked()

    # --- health card mutators ([R3]: locked + persisted; callers must NOT mutate
    # the copies returned by get()/items() — those changes never reach disk) ---

    def set_health_card(self, session_id: str, card_id: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.health_card_id = card_id
                self._save_locked()

    def set_title(self, session_id: str, title: str, source: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.cached_title = title
                session.title_source = source
                self._save_locked()

    def set_stopped(
        self,
        session_id: str,
        reason: str = "completed",
        *,
        interrupt_reason: str = "",
        running_since: float = 0.0,
        preserve_terminal: bool = True,
    ) -> None:
        """Record a stopped state.

        `preserve_terminal` keeps a later Stop hook from overwriting an earlier
        timeout/user interrupt or agent-exit style terminal state.
        """
        reason = reason or "unknown"
        terminal_reasons = {"interrupted", "agent_error", "agent_exited"}
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            if (
                preserve_terminal
                and session.status == "stopped"
                and session.stop_reason in terminal_reasons
                and reason == "completed"
            ):
                return
            changed = (
                session.status != "stopped"
                or session.stop_reason != reason
                or session.interrupt_reason != interrupt_reason
                or session.running_since != running_since
            )
            if changed:
                session.status = "stopped"
                session.stop_reason = reason
                session.interrupt_reason = interrupt_reason
                session.running_since = running_since
                self._save_locked()

    def mark_waiting(self, session_id: str, reason: str, started_at: float) -> None:
        """Record a human-waiting state that is still timeout-watchable."""
        if reason not in ("permission_request", "ask_user_question"):
            reason = "unknown"
        self.set_stopped(
            session_id,
            reason,
            running_since=started_at,
            preserve_terminal=False,
        )

    def set_status(self, session_id: str, status: str) -> None:
        """Compatibility wrapper for the pre-status/stop_reason state model."""
        if status in ("", "running"):
            self.start_running(session_id, time.time())
        elif status == "timeout":
            self.set_stopped(session_id, "interrupted", interrupt_reason="timeout")
        elif status == "stopped":
            self.set_stopped(session_id, "completed")
        else:
            self.set_stopped(session_id, "unknown")

    def start_running(self, session_id: str, started_at: float) -> None:
        """Record running state and reset the running timeout timer."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            changed = (
                session.status != "running"
                or session.stop_reason
                or session.interrupt_reason
                or session.running_since != started_at
            )
            if changed:
                session.status = "running"
                session.stop_reason = ""
                session.interrupt_reason = ""
                session.running_since = started_at
                self._save_locked()

    def clear_running(self, session_id: str) -> None:
        """Clear the timeout-watchable period without changing the status."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None and session.running_since:
                session.running_since = 0.0
                self._save_locked()


    def resolve_pending_tty(self, msg_id: str) -> str | None:
        with self._lock:
            return self._pending_msg_to_tty.get(msg_id)

    # --- Failed-delivery redelivery queue ---

    def add_redelivery(self, session_id: str, text: str, key: tuple | None = None):
        """Stash a reply whose Feishu delivery failed, to retry on the next hook.

        No-op if the session is unknown (nothing to anchor a thread on). A repeat
        stash of the same dedupe ``key`` (codex fires Stop twice per turn, so a
        network blip stashes both) is collapsed so redelivery sends it once. The
        backlog is bounded to the most recent ``_MAX_REDELIVERY`` entries.
        """
        key_list = [str(k) for k in key] if key else None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            if key_list is not None and any(
                e.get("key") == key_list for e in session.pending_redelivery
            ):
                return  # same turn already stashed
            session.pending_redelivery.append({"key": key_list, "text": text})
            if len(session.pending_redelivery) > _MAX_REDELIVERY:
                dropped = len(session.pending_redelivery) - _MAX_REDELIVERY
                session.pending_redelivery = session.pending_redelivery[-_MAX_REDELIVERY:]
                logger.warning(
                    "Redelivery backlog for session %s exceeded %d; dropped %d oldest reply(ies)",
                    session_id[:8], _MAX_REDELIVERY, dropped,
                )
            self._save_locked()

    def take_redelivery(self, session_id: str) -> list[dict]:
        """Remove and return all stashed replies for a session, oldest first.

        Each entry is ``{"key": list|None, "text": str}``.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or not session.pending_redelivery:
                return []
            items = [dict(x) for x in session.pending_redelivery]
            session.pending_redelivery = []
            self._save_locked()
            return items
