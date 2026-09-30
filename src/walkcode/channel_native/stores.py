"""Channel-native persistent state: authorization, sessions, interactions, outbox, inbound ledger, HITL and the JSON state store."""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import (
    ActorRef,
    agent_session_id,
    AttachmentRef,
    AuthorizationResult,
    BindingKey,
    BindingResolution,
    BlockedInput,
    BlockedReason,
    ChannelBinding,
    ControlResult,
    DeliveryStatus,
    _log_degrade,
    Session,
    _session_has_durable_resume_ref,
    _session_is_channel_revival_candidate,
    _session_is_external_tui_takeover_candidate,
    SessionRole,
    SessionSummary,
    _STRUCTURED_TRANSPORT_KINDS,
    SubmitResult,
    TakeoverError,
    TakeoverPhase,
    TakeoverTransaction,
    TurnInput,
    WriterOwner,
)


# Session retention (maintenance compaction). Stopped sessions used to be kept
# forever — with their bindings, grants, takeovers and blocked user text — and
# the whole ledger is rewritten on every save. A stopped session is dropped
# once it has been idle for its window and nothing still points at it.
#
# The long window covers sessions a message in their topic can still bring
# back: ADR 0054 channel revival (involuntary stop + durable resume ref) and
# TUI-observed sessions, which a topic reply resumes through the takeover
# prompt when their resume ref is durable. Everything else (archived, no
# topic, no resumable identity) can never continue and gets the short window.
SESSION_REVIVABLE_RETENTION_SECONDS = 90 * 86400.0
SESSION_FINAL_RETENTION_SECONDS = 7 * 86400.0
# A structured session left "running" with no worker and no activity this long
# is stopped as idle_expired: a reply in its topic still revives it (ADR 0054),
# and the stopped-session retention above can finally drop it. Without this
# they stayed "running" forever (work-claude: 81, idle 23–90 days).
SESSION_IDLE_EXPIRY_SECONDS = 30 * 86400.0
_IDLE_LIFECYCLE_STATES = frozenset({"IDLE", "ERROR_RECOVERABLE"})


def _session_revivable_from_topic(session: Session) -> bool:
    if session.archived_at or session.channel_binding is None:
        return False
    if _session_is_channel_revival_candidate(session):
        return True
    return _session_is_external_tui_takeover_candidate(session) and _session_has_durable_resume_ref(session)


def _session_last_activity(session: Session) -> float:
    stamps = [
        session.created_at,
        session.running_since,
        session.last_progress_at,
        session.last_user_input_at,
        session.title_refreshed_at,
        session.archived_at,
    ]
    # A topic reply to a stopped TUI session only records a blocked input
    # (and a takeover prompt); it must count as activity too.
    stamps.extend(blocked.created_at for blocked in session.blocked_inputs.values())
    return max(stamps)


class AuthorizationStore:
    def __init__(self) -> None:
        self._roles: dict[str, dict[tuple[str, str], str]] = {}

    def grant(self, session_id: str, actor: ActorRef, role: str) -> None:
        if role not in {
            SessionRole.OWNER,
            SessionRole.COLLABORATOR,
            SessionRole.REVIEWER,
            SessionRole.ADMIN,
        }:
            raise ValueError(f"unknown session role: {role}")
        self._roles.setdefault(session_id, {})[(actor.channel_kind, actor.actor_id)] = role

    def drop_sessions(self, session_ids: Iterable[str]) -> int:
        """Forget every grant of the given sessions; returns how many sessions had any."""
        return sum(1 for session_id in session_ids if self._roles.pop(session_id, None) is not None)

    def role_for(self, session_id: str, actor: ActorRef) -> str:
        return self._roles.get(session_id, {}).get((actor.channel_kind, actor.actor_id), "")

    def can_submit(self, session_id: str, actor: ActorRef) -> AuthorizationResult:
        role = self.role_for(session_id, actor)
        if role in {SessionRole.OWNER, SessionRole.COLLABORATOR, SessionRole.ADMIN}:
            return AuthorizationResult(True, role=role)
        return AuthorizationResult(False, BlockedReason.UNAUTHORIZED, role=role)

    def can_decide_permission(
        self,
        session_id: str,
        actor: ActorRef,
        *,
        high_risk: bool = False,
    ) -> AuthorizationResult:
        role = self.role_for(session_id, actor)
        if high_risk:
            allowed = role in {SessionRole.OWNER, SessionRole.ADMIN}
        else:
            allowed = role in {SessionRole.OWNER, SessionRole.COLLABORATOR, SessionRole.ADMIN}
        if allowed:
            return AuthorizationResult(True, role=role)
        return AuthorizationResult(False, BlockedReason.UNAUTHORIZED, role=role)

    def can_takeover(self, session_id: str, actor: ActorRef) -> AuthorizationResult:
        role = self.role_for(session_id, actor)
        if role in {SessionRole.OWNER, SessionRole.ADMIN}:
            return AuthorizationResult(True, role=role)
        return AuthorizationResult(False, BlockedReason.UNAUTHORIZED, role=role)

    def can_control_session(
        self,
        session_id: str,
        actor: ActorRef,
        *,
        action: str,
    ) -> AuthorizationResult:
        role = self.role_for(session_id, actor)
        if role in {SessionRole.OWNER, SessionRole.ADMIN}:
            return AuthorizationResult(True, role=role)
        return AuthorizationResult(False, BlockedReason.UNAUTHORIZED, role=role)

    def to_dict(self) -> dict[str, Any]:
        grants = []
        for session_id, roles in self._roles.items():
            for (channel_kind, actor_id), role in roles.items():
                grants.append(
                    {
                        "session_id": session_id,
                        "channel_kind": channel_kind,
                        "actor_id": actor_id,
                        "role": role,
                    }
                )
        return {"grants": grants}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuthorizationStore":
        # Files written before v0.14.36 also carry an "audit" list: an
        # append-only log of every grant that nothing ever read. Ignored.
        store = cls()
        for grant in data.get("grants", []):
            if not isinstance(grant, dict):
                continue
            session_id = str(grant.get("session_id", ""))
            channel_kind = str(grant.get("channel_kind", ""))
            actor_id = str(grant.get("actor_id", ""))
            role = str(grant.get("role", ""))
            if session_id and channel_kind and actor_id and role:
                store._roles.setdefault(session_id, {})[(channel_kind, actor_id)] = role
        return store


# Rolling tool-progress card state is turn-scoped; persisting it would make a
# restarted process edit (and pollute) a previous run's card.
_TRANSIENT_BINDING_KEYS = ("tool_progress_message_id", "tool_progress_lines")


def _binding_to_dict(binding: ChannelBinding | None) -> dict[str, Any] | None:
    if binding is None:
        return None
    capabilities = {
        key: value
        for key, value in binding.capabilities.items()
        if key not in _TRANSIENT_BINDING_KEYS
    }
    return {
        "channel_kind": binding.channel_kind,
        "account_id": binding.account_id,
        "chat_id": binding.chat_id,
        "thread_id": binding.thread_id,
        "root_message_id": binding.root_message_id,
        "last_message_id": binding.last_message_id,
        "health_message_id": binding.health_message_id,
        "capabilities": capabilities,
    }


def _binding_from_dict(data: dict[str, Any] | None) -> ChannelBinding | None:
    if not data:
        return None
    capabilities = {
        key: value
        for key, value in dict(data.get("capabilities", {})).items()
        if key not in _TRANSIENT_BINDING_KEYS
    }
    return ChannelBinding(
        channel_kind=str(data.get("channel_kind", "")),
        account_id=str(data.get("account_id", "")),
        chat_id=str(data.get("chat_id", "")),
        thread_id=str(data.get("thread_id", "")),
        root_message_id=str(data.get("root_message_id", "")),
        last_message_id=str(data.get("last_message_id", "")),
        health_message_id=str(data.get("health_message_id", "")),
        capabilities=capabilities,
    )


def _actor_to_dict(actor: ActorRef | None) -> dict[str, Any] | None:
    if actor is None:
        return None
    return {
        "channel_kind": actor.channel_kind,
        "actor_id": actor.actor_id,
        "display_name": actor.display_name,
    }


def _actor_from_dict(data: dict[str, Any] | None) -> ActorRef | None:
    if not data:
        return None
    return ActorRef(
        channel_kind=str(data.get("channel_kind", "")),
        actor_id=str(data.get("actor_id", "")),
        display_name=str(data.get("display_name", "")),
    )


def _attachment_to_dict(attachment: AttachmentRef) -> dict[str, Any]:
    return {
        "source_id": attachment.source_id,
        "mime": attachment.mime,
        "local_path": attachment.local_path,
        "source_message_id": attachment.source_message_id,
    }


def _attachment_from_dict(data: dict[str, Any]) -> AttachmentRef:
    return AttachmentRef(
        source_id=str(data.get("source_id", "")),
        mime=str(data.get("mime", "")),
        local_path=str(data.get("local_path", "")),
        source_message_id=str(data.get("source_message_id", "")),
    )


def _writer_owner_to_dict(owner: WriterOwner | None) -> dict[str, Any] | None:
    if owner is None:
        return None
    return {
        "kind": owner.kind,
        "transport_kind": owner.transport_kind,
        "actor_id": owner.actor_id,
        "external_ref": dict(owner.external_ref),
        "acquired_at": owner.acquired_at,
    }


def _writer_owner_from_dict(data: dict[str, Any] | None) -> WriterOwner | None:
    if not data:
        return None
    return WriterOwner(
        kind=data.get("kind", "none"),
        transport_kind=str(data.get("transport_kind", "")),
        actor_id=str(data.get("actor_id", "")),
        external_ref=dict(data.get("external_ref", {})),
        acquired_at=float(data.get("acquired_at", 0.0)),
    )


def _blocked_input_to_dict(blocked: BlockedInput) -> dict[str, Any]:
    return {
        "blocked_input_id": blocked.blocked_input_id,
        "session_id": blocked.session_id,
        "actor": _actor_to_dict(blocked.actor),
        "text": blocked.text,
        "attachments": [_attachment_to_dict(item) for item in blocked.attachments],
        "idempotency_key": blocked.idempotency_key,
        "state": blocked.state,
        "created_at": blocked.created_at,
        "submit_after_takeover": blocked.submit_after_takeover,
    }


def _blocked_input_from_dict(data: dict[str, Any]) -> BlockedInput:
    actor = _actor_from_dict(data.get("actor")) or ActorRef("", "")
    return BlockedInput(
        blocked_input_id=str(data.get("blocked_input_id", "")),
        session_id=str(data.get("session_id", "")),
        actor=actor,
        text=str(data.get("text", "")),
        attachments=[
            _attachment_from_dict(item) for item in data.get("attachments", []) if isinstance(item, dict)
        ],
        idempotency_key=str(data.get("idempotency_key", "")),
        state=data.get("state", "blocked"),
        created_at=float(data.get("created_at", 0.0)),
        submit_after_takeover=bool(data.get("submit_after_takeover", True)),
    )


def _session_to_dict(session: Session) -> dict[str, Any]:
    return {
        "schema_version": session.schema_version,
        "session_id": session.session_id,
        "transport_kind": session.transport_kind,
        "transport_ref": dict(session.transport_ref),
        "cwd": session.cwd,
        "channel_binding": _binding_to_dict(session.channel_binding),
        "lifecycle_state": session.lifecycle_state,
        "writer_owner": _writer_owner_to_dict(session.writer_owner),
        "generation": session.generation,
        "last_event_seq": session.last_event_seq,
        "blocked_inputs": {
            key: _blocked_input_to_dict(value) for key, value in session.blocked_inputs.items()
        },
        "cached_title": session.cached_title,
        "title_source": session.title_source,
        "title_refreshed_at": session.title_refreshed_at,
        "status": session.status,
        "stop_reason": session.stop_reason,
        "running_since": session.running_since,
        "last_progress_at": session.last_progress_at,
        "last_progress_event": session.last_progress_event,
        "last_user_input_at": session.last_user_input_at,
        "archived_at": session.archived_at,
        "archived_by": session.archived_by,
        "archive_reason": session.archive_reason,
        "model": session.model,
        "last_usage": dict(session.last_usage),
        "created_at": session.created_at,
        "background_tasks": [dict(task) for task in session.background_tasks],
    }


def _session_from_dict(data: dict[str, Any]) -> Session:
    transport_kind = str(data.get("transport_kind", ""))
    if transport_kind == "claude_daemon":
        # ADR 0068: the Claude daemon transport is gone; such a session was a
        # TUI session driven through it, which is what external_tui means.
        transport_kind = "external_tui"
    return Session(
        schema_version=int(data.get("schema_version", 1)),
        session_id=str(data.get("session_id", "")),
        transport_kind=transport_kind,
        transport_ref=dict(data.get("transport_ref", {})),
        cwd=str(data.get("cwd", "")),
        channel_binding=_binding_from_dict(data.get("channel_binding")),
        lifecycle_state=str(data.get("lifecycle_state", "NEW")),
        writer_owner=_writer_owner_from_dict(data.get("writer_owner")),
        generation=int(data.get("generation", 0)),
        last_event_seq=int(data.get("last_event_seq", 0)),
        blocked_inputs={
            str(key): _blocked_input_from_dict(value)
            for key, value in data.get("blocked_inputs", {}).items()
            if isinstance(value, dict)
        },
        cached_title=str(data.get("cached_title", "")),
        title_source=str(data.get("title_source", "")),
        title_refreshed_at=float(data.get("title_refreshed_at", 0.0)),
        status=data.get("status", "running"),
        stop_reason=str(data.get("stop_reason", "")),
        running_since=float(data.get("running_since", 0.0)),
        last_progress_at=float(data.get("last_progress_at", 0.0)),
        last_user_input_at=float(data.get("last_user_input_at", 0.0)),
        last_progress_event=str(data.get("last_progress_event", "")),
        archived_at=float(data.get("archived_at", 0.0)),
        archived_by=str(data.get("archived_by", "")),
        archive_reason=str(data.get("archive_reason", "")),
        model=str(data.get("model", "")),
        last_usage=dict(data.get("last_usage", {}) or {}),
        created_at=float(data.get("created_at", 0.0)),
        background_tasks=[
            dict(task)
            for task in (data.get("background_tasks") or [])
            if isinstance(task, dict)
        ],
    )


def _delivery_to_dict(item: DeliveryItem) -> dict[str, Any]:
    return {
        "delivery_id": item.delivery_id,
        "seq": item.seq,
        "channel_binding_key": list(item.channel_binding_key),
        "view_model": dict(item.view_model),
        "idempotency_key": item.idempotency_key,
        "attempt_count": item.attempt_count,
        "created_at": item.created_at,
        "next_attempt_at": item.next_attempt_at,
        "last_error": item.last_error,
        "finished_at": item.finished_at,
        "claim_owner": item.claim_owner,
        "claim_until": item.claim_until,
        "message_id": item.message_id,
    }


def _delivery_from_dict(data: dict[str, Any]) -> DeliveryItem:
    return DeliveryItem(
        delivery_id=str(data.get("delivery_id", "")),
        seq=int(data.get("seq", 0)),
        channel_binding_key=tuple(data.get("channel_binding_key", ("", "", "", "", ""))),  # type: ignore[arg-type]
        view_model=dict(data.get("view_model", {})),
        idempotency_key=str(data.get("idempotency_key", "")),
        attempt_count=int(data.get("attempt_count", 0)),
        created_at=float(data.get("created_at", 0.0)),
        next_attempt_at=float(data.get("next_attempt_at", 0.0)),
        last_error=str(data.get("last_error", "")),
        finished_at=float(data.get("finished_at", 0.0)),
        claim_owner=str(data.get("claim_owner", "")),
        claim_until=float(data.get("claim_until", 0.0)),
        message_id=str(data.get("message_id", "")),
    )


class SessionRegistry:
    def __init__(self, *, now: Callable[[], float] = time.time):
        self._now = now
        self._sessions: dict[str, Session] = {}
        self._binding_to_session: dict[BindingKey, str] = {}
        self._takeovers: dict[str, TakeoverTransaction] = {}

    def get(self, session_id: str) -> Session:
        return self._sessions[session_id]

    def iter_sessions(self) -> Iterator[Session]:
        yield from self._sessions.values()

    def resolve_binding(self, key: BindingKey) -> str | None:
        return self._binding_to_session.get(key)

    def resolve_active_binding(
        self,
        key: BindingKey,
        *,
        revival_eligible: Callable[[Session], bool] | None = None,
    ) -> BindingResolution:
        """Resolve a binding key to the session a message should reach.

        ``revival_eligible`` opts a call site into ADR 0054 revival
        resolution: stopped-but-revivable sessions are considered only as a
        SECOND layer, after the existing active/takeover candidates, and only
        when the predicate confirms the session could actually be revived
        (transport wired + resumable). Call sites that never revive keep the
        pre-0054 semantics by omitting it.
        """
        exact = self.resolve_binding(key)
        if exact is not None:
            channel_kind, account_id, chat_id, thread_id, root_message_id = key
            session = self._sessions.get(exact)
            if session is not None and session.status == "stopped" and not thread_id and not root_message_id:
                return BindingResolution()
            return BindingResolution(session_id=exact)
        channel_kind, account_id, chat_id, thread_id, root_message_id = key
        if root_message_id and not thread_id:
            return BindingResolution()
        candidates: list[str] = []
        revival_candidates: list[tuple[float, str]] = []
        for candidate_key, session_id in self._binding_to_session.items():
            candidate_channel, candidate_account, candidate_chat, candidate_thread, _candidate_root = candidate_key
            if (
                candidate_channel,
                candidate_account,
                candidate_chat,
                candidate_thread,
            ) != (channel_kind, account_id, chat_id, thread_id):
                continue
            session = self._sessions.get(session_id)
            if session is None:
                continue
            if session.status != "stopped" or (
                bool(thread_id) and _session_is_external_tui_takeover_candidate(session)
            ):
                candidates.append(session_id)
            elif (
                revival_eligible is not None
                and bool(thread_id)
                and _session_is_channel_revival_candidate(session)
                and revival_eligible(session)
            ):
                revival_candidates.append((session.last_progress_at, session_id))
        unique_candidates = sorted(set(candidates))
        if len(unique_candidates) == 1:
            return BindingResolution(session_id=unique_candidates[0])
        if len(unique_candidates) > 1:
            return BindingResolution(reason=BlockedReason.AMBIGUOUS_SESSION)
        if revival_candidates:
            # Revival never produces a chooser (the chooser filters stopped
            # sessions and would come up empty): pick the most recently
            # active candidate deterministically.
            revival_candidates.sort()
            return BindingResolution(session_id=revival_candidates[-1][1])
        return BindingResolution()

    def update_channel_binding(self, session_id: str, binding: ChannelBinding) -> None:
        session = self._sessions[session_id]
        previous = session.channel_binding
        if previous is not None:
            self._binding_to_session.pop(previous.key(), None)
        session.channel_binding = binding
        self._binding_to_session[binding.key()] = session_id

    def find_by_resume_ref(self, *, transport_kind: str, resume_ref: dict[str, Any]) -> str | None:
        target = agent_session_id(transport_kind, resume_ref)
        if not target:
            return None
        for session_id, session in self._sessions.items():
            refs = [session.transport_ref]
            if session.writer_owner is not None:
                refs.append(session.writer_owner.external_ref)
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                nested = ref.get("resume_ref")
                if isinstance(nested, dict):
                    if agent_session_id(transport_kind, nested) == target:
                        return session_id
                if agent_session_id(transport_kind, ref) == target:
                    return session_id
        return None

    def list_sessions(
        self,
        *,
        channel_kind: str = "",
        account_id: str = "",
        chat_id: str = "",
        thread_id: str = "",
        include_archived: bool = False,
    ) -> list[SessionSummary]:
        summaries: list[SessionSummary] = []
        for session in self._sessions.values():
            if session.archived_at and not include_archived:
                continue
            binding = session.channel_binding
            if binding is None:
                continue
            if channel_kind and binding.channel_kind != channel_kind:
                continue
            if account_id and binding.account_id != account_id:
                continue
            if chat_id and binding.chat_id != chat_id:
                continue
            if thread_id and binding.thread_id != thread_id:
                continue
            summaries.append(
                SessionSummary(
                    session_id=session.session_id,
                    channel_kind=binding.channel_kind,
                    account_id=binding.account_id,
                    chat_id=binding.chat_id,
                    thread_id=binding.thread_id,
                    root_message_id=binding.root_message_id,
                    status=session.status,
                    lifecycle_state=session.lifecycle_state,
                    transport_kind=session.transport_kind,
                    cwd=session.cwd,
                    title=session.cached_title,
                    created_at=session.created_at,
                    archived_at=session.archived_at,
                    archived_by=session.archived_by,
                )
            )
        return sorted(summaries, key=lambda item: (item.created_at, item.session_id))

    def archive_session(self, session_id: str, *, actor: ActorRef, reason: str) -> ControlResult:
        session = self._sessions.get(session_id)
        if session is None:
            return ControlResult(False, BlockedReason.NOT_FOUND)
        if session.status != "stopped":
            return ControlResult(False, BlockedReason.SESSION_RUNNING)
        if not session.archived_at:
            session.archived_at = self._now()
            session.archived_by = actor.actor_id
            session.archive_reason = reason
        return ControlResult(True, state="archived")

    def expire_idle_sessions(self, *, is_live: Callable[[Session], bool]) -> list[str]:
        """Stop long-idle structured sessions whose worker is gone.

        ``is_live`` guards against stopping a session whose worker is still
        attached. Returns the expired session ids.
        """
        now = self._now()
        expired = []
        for session_id, session in self._sessions.items():
            if (
                session.status == "running"
                and session.transport_kind in _STRUCTURED_TRANSPORT_KINDS
                and session.lifecycle_state in _IDLE_LIFECYCLE_STATES
                and now - _session_last_activity(session) >= SESSION_IDLE_EXPIRY_SECONDS
                and not is_live(session)
            ):
                session.status = "stopped"
                session.stop_reason = "idle_expired"
                session.lifecycle_state = "STOPPED"
                session.writer_owner = WriterOwner(kind="none")
                expired.append(session_id)
        return expired

    def prune_stopped_sessions(self, *, referenced: set[str]) -> list[str]:
        """Drop stopped sessions past their retention window (see
        SESSION_REVIVABLE_RETENTION_SECONDS); ``referenced`` ids are kept.

        Removes the sessions' binding index entries and takeovers too; blocked
        inputs live inside the session. Returns the removed session ids.
        """
        now = self._now()
        removed: set[str] = set()
        for session_id, session in self._sessions.items():
            if session.status != "stopped" or session_id in referenced:
                continue
            window = (
                SESSION_REVIVABLE_RETENTION_SECONDS
                if _session_revivable_from_topic(session)
                else SESSION_FINAL_RETENTION_SECONDS
            )
            if now - _session_last_activity(session) >= window:
                removed.add(session_id)
        if not removed:
            return []
        for session_id in removed:
            del self._sessions[session_id]
        self._binding_to_session = {
            key: session_id
            for key, session_id in self._binding_to_session.items()
            if session_id not in removed
        }
        self._takeovers = {
            takeover_id: tx
            for takeover_id, tx in self._takeovers.items()
            if tx.session_id not in removed
        }
        return sorted(removed)

    def binding_keys_by_session(self) -> dict[str, set[BindingKey]]:
        keys: dict[str, set[BindingKey]] = {}
        for key, session_id in self._binding_to_session.items():
            keys.setdefault(session_id, set()).add(key)
        for session_id, session in self._sessions.items():
            if session.channel_binding is not None:
                keys.setdefault(session_id, set()).add(session.channel_binding.key())
        return keys

    def create_structured_session(
        self,
        *,
        session_id: str | None = None,
        binding: ChannelBinding,
        transport_kind: str,
        transport_ref: dict[str, Any],
        cwd: str,
        owner: ActorRef,
    ) -> Session:
        sid = session_id or f"sess-{uuid.uuid4().hex}"
        now = self._now()
        session = Session(
            schema_version=1,
            session_id=sid,
            transport_kind=transport_kind,
            transport_ref=dict(transport_ref),
            cwd=cwd,
            channel_binding=binding,
            lifecycle_state="ACTIVE",
            writer_owner=WriterOwner(
                kind="orchestrator",
                transport_kind=transport_kind,
                actor_id=owner.actor_id,
                acquired_at=now,
            ),
            generation=0,
            running_since=now,
            last_progress_at=now,
            last_progress_event="session.started",
            created_at=now,
        )
        self._sessions[sid] = session
        self._binding_to_session[binding.key()] = sid
        return session

    def create_observed_session(
        self,
        *,
        session_id: str,
        binding: ChannelBinding,
        cwd: str,
        external_ref: dict[str, Any],
        owner: ActorRef,
    ) -> Session:
        now = self._now()
        session = Session(
            schema_version=1,
            session_id=session_id,
            transport_kind="external_tui",
            transport_ref=dict(external_ref),
            cwd=cwd,
            channel_binding=binding,
            lifecycle_state="EXTERNAL_OBSERVED_READONLY",
            writer_owner=WriterOwner(
                kind="external_tui",
                actor_id=owner.actor_id,
                external_ref=dict(external_ref),
                acquired_at=now,
            ),
            generation=0,
            last_progress_at=now,
            last_progress_event="external_tui.observed",
            created_at=now,
        )
        self._sessions[session_id] = session
        self._binding_to_session[binding.key()] = session_id
        return session

    def validate_submit(self, session_id: str, generation: int) -> SubmitResult:
        session = self._sessions.get(session_id)
        if session is None:
            return SubmitResult(False, BlockedReason.NOT_FOUND)
        if generation != session.generation:
            return SubmitResult(False, BlockedReason.STALE_GENERATION)
        if session.status == "stopped":
            return SubmitResult(False, BlockedReason.SESSION_STOPPED)
        if session.writer_owner and session.writer_owner.kind == "external_tui":
            return SubmitResult(False, BlockedReason.EXTERNAL_TUI_READONLY)
        # ADR 0059: no lease-expiry veto. The lease is only stamped at writer
        # (re)acquire and never renewed while a turn runs, so any mid-turn
        # message arriving after the TTL used to hit LEASE_EXPIRED — and on
        # the Lark WS ingress (fire-and-forget ack, no redelivery) that meant
        # a silent, permanent message drop. Real double-writer protection is
        # the generation fence above, the external-TUI ownership check, and
        # the transport's atomic close-old→verify-dead→create-new resume
        # barrier; worker liveness is proven by the submit itself
        # (TransportUnavailable → resume fallback). The lease itself was
        # write-only after that and has been removed from the state model.
        return SubmitResult(True)

    def acquire_structured_writer(
        self,
        session_id: str,
        *,
        transport_kind: str,
        transport_ref: dict[str, Any],
        owner: ActorRef,
    ) -> SubmitResult:
        session = self._sessions.get(session_id)
        if session is None:
            return SubmitResult(False, BlockedReason.NOT_FOUND)
        if session.status == "stopped":
            return SubmitResult(False, BlockedReason.SESSION_STOPPED)
        now = self._now()
        session.transport_kind = transport_kind
        session.transport_ref = dict(transport_ref)
        session.lifecycle_state = "ACTIVE"
        session.writer_owner = WriterOwner(
            kind="orchestrator",
            transport_kind=transport_kind,
            actor_id=owner.actor_id,
            acquired_at=now,
        )
        session.last_progress_at = now
        session.last_progress_event = "writer.reacquired"
        return SubmitResult(True)

    def revive_stopped_structured_session(self, session_id: str) -> SubmitResult:
        """Bring a stopped structured session back for a channel-driven submit.

        ADR 0054: every runtime restart sweeps headless sessions to
        "stopped", and a later channel message used to dead-end at 会话已结束
        even though the transcript and resume credentials are intact. This is
        takeover minus the kill: bump the generation (fences any stale
        drains), reset to IDLE, and let the normal resume-for-submit path
        spawn a fresh worker. The caller is responsible for verifying a
        durable resume ref and transport capability BEFORE calling, and for
        reverting via mark_revive_failed if the resume then fails.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return SubmitResult(False, BlockedReason.NOT_FOUND)
        if session.status != "stopped":
            return SubmitResult(False, "not_stopped")
        if session.archived_at:
            # An archived session is hidden from the session list; reviving
            # it would produce a running-but-invisible record.
            return SubmitResult(False, "archived")
        if session.transport_kind not in _STRUCTURED_TRANSPORT_KINDS:
            return SubmitResult(False, "not_structured")
        now = self._now()
        session.generation += 1
        session.status = "running"
        session.stop_reason = ""
        session.lifecycle_state = "IDLE"
        session.writer_owner = None
        session.background_tasks = []
        session.last_progress_at = now
        session.last_progress_event = "session.revived_by_channel"
        return SubmitResult(True)

    def mark_revive_failed(self, session_id: str) -> None:
        """Revert a channel revival whose resume never produced a worker.

        Leaving the session "running" with no writer after a failed revive
        would be a phantom-alive record (status card says running, nothing
        serves it). The generation stays bumped — that is harmless and keeps
        the fence monotonic.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return
        session.status = "stopped"
        session.stop_reason = "revive_failed"
        session.lifecycle_state = "STOPPED"
        session.writer_owner = None
        session.last_progress_at = self._now()
        session.last_progress_event = "session.revive_failed"

    def handoff_to_external_tui(
        self,
        session_id: str,
        *,
        generation: int,
        owner: ActorRef,
        resume_ref: dict[str, Any],
        external_ref: dict[str, Any],
    ) -> SubmitResult:
        session = self._sessions.get(session_id)
        if session is None:
            return SubmitResult(False, BlockedReason.NOT_FOUND)
        if generation != session.generation:
            return SubmitResult(False, BlockedReason.STALE_GENERATION)
        if session.status == "stopped":
            return SubmitResult(False, BlockedReason.SESSION_STOPPED)
        now = self._now()
        ref = dict(external_ref)
        ref["resume_ref"] = dict(resume_ref)
        session.generation += 1
        session.transport_kind = "external_tui"
        session.transport_ref = ref
        session.lifecycle_state = "EXTERNAL_OBSERVED_READONLY"
        session.writer_owner = WriterOwner(
            kind="external_tui",
            actor_id=owner.actor_id,
            external_ref=ref,
            acquired_at=now,
        )
        session.last_progress_at = now
        session.last_progress_event = "external_tui.claimed"
        return SubmitResult(True)

    def block_input(
        self,
        session_id: str,
        *,
        actor: ActorRef,
        turn: TurnInput,
        generation: int,
    ) -> SubmitResult:
        session = self._sessions[session_id]
        if generation != session.generation:
            return SubmitResult(False, BlockedReason.STALE_GENERATION)
        if not _session_is_external_tui_takeover_candidate(session):
            return SubmitResult(False, BlockedReason.NOT_EXTERNAL_TUI)
        now = self._now()
        blocked_id = f"blocked-{uuid.uuid4().hex}"
        session.blocked_inputs[blocked_id] = BlockedInput(
            blocked_input_id=blocked_id,
            session_id=session_id,
            actor=actor,
            text=turn.text,
            attachments=list(turn.attachments),
            idempotency_key=f"blocked:{blocked_id}",
            state="blocked",
            created_at=now,
        )
        return SubmitResult(False, BlockedReason.EXTERNAL_TUI_READONLY, blocked_input_id=blocked_id)

    def request_takeover(
        self,
        session_id: str,
        blocked_input_id: str,
        *,
        requested_by: ActorRef,
        generation: int,
    ) -> TakeoverTransaction:
        session = self._require_takeover_session(session_id, generation)
        blocked = session.blocked_inputs.get(blocked_input_id)
        if blocked is None:
            raise TakeoverError(BlockedReason.NOT_FOUND)
        if blocked.state != "blocked":
            raise TakeoverError(f"blocked input is {blocked.state}")
        tx = TakeoverTransaction(
            takeover_id=f"takeover-{uuid.uuid4().hex}",
            session_id=session_id,
            blocked_input_id=blocked_input_id,
            requested_by=requested_by,
            requested_generation=generation,
            phase=TakeoverPhase.PROMPTED,
            created_at=self._now(),
        )
        self._takeovers[tx.takeover_id] = tx
        return tx

    def request_takeover_only(
        self,
        session_id: str,
        *,
        requested_by: ActorRef,
        generation: int,
    ) -> TakeoverTransaction:
        session = self._require_takeover_session(session_id, generation)
        existing = self._find_takeover_only_transaction(session_id, generation)
        if existing is not None:
            return existing
        now = self._now()
        blocked_id = f"takeover-only-{uuid.uuid4().hex}"
        session.blocked_inputs[blocked_id] = BlockedInput(
            blocked_input_id=blocked_id,
            session_id=session_id,
            actor=requested_by,
            text="",
            attachments=[],
            idempotency_key=f"takeover-only:{blocked_id}",
            state="blocked",
            created_at=now,
            submit_after_takeover=False,
        )
        tx = TakeoverTransaction(
            takeover_id=f"takeover-{uuid.uuid4().hex}",
            session_id=session_id,
            blocked_input_id=blocked_id,
            requested_by=requested_by,
            requested_generation=generation,
            phase=TakeoverPhase.PROMPTED,
            created_at=now,
        )
        self._takeovers[tx.takeover_id] = tx
        return tx

    def _find_takeover_only_transaction(self, session_id: str, generation: int) -> TakeoverTransaction | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        candidates = [
            tx
            for tx in self._takeovers.values()
            if tx.session_id == session_id and tx.requested_generation == generation
        ]
        candidates.sort(key=lambda item: item.created_at)
        for tx in candidates:
            blocked = session.blocked_inputs.get(tx.blocked_input_id)
            if blocked is None:
                continue
            if blocked.submit_after_takeover:
                continue
            return tx
        return None

    def authorize_takeover(
        self,
        takeover_id: str,
        *,
        approved_by: ActorRef,
        resume_ref: dict[str, Any] | None,
    ) -> TakeoverTransaction:
        tx = self._takeovers[takeover_id]
        self._require_takeover_session(tx.session_id, tx.requested_generation)
        if tx.phase != TakeoverPhase.PROMPTED:
            raise TakeoverError(f"takeover is {tx.phase}")
        tx.approved_by = approved_by
        tx.resume_ref = dict(resume_ref) if resume_ref else None
        tx.authorized_at = self._now()
        if not tx.resume_ref:
            tx.phase = TakeoverPhase.MANUAL_ONLY
            tx.reason = "no structured resume reference available"
        else:
            tx.phase = TakeoverPhase.AUTHORIZED
        return tx

    def fail_takeover(self, takeover_id: str, *, reason: str) -> TakeoverTransaction:
        tx = self._takeovers[takeover_id]
        if tx.phase == TakeoverPhase.COMPLETED:
            raise TakeoverError(f"takeover is {tx.phase}")
        tx.phase = TakeoverPhase.FAILED
        tx.reason = reason
        return tx

    def complete_takeover(
        self,
        takeover_id: str,
        *,
        transport_kind: str,
        transport_ref: dict[str, Any],
    ) -> TakeoverTransaction:
        tx = self._takeovers[takeover_id]
        session = self._require_takeover_session(tx.session_id, tx.requested_generation)
        if tx.phase != TakeoverPhase.AUTHORIZED:
            raise TakeoverError(f"takeover is {tx.phase}")
        blocked = session.blocked_inputs.get(tx.blocked_input_id)
        if blocked is None or blocked.state != "blocked":
            raise TakeoverError("blocked input is not pending")

        now = self._now()
        new_generation = session.generation + 1
        session.transport_kind = transport_kind
        session.transport_ref = dict(transport_ref)
        session.status = "running"
        session.stop_reason = ""
        session.lifecycle_state = "ACTIVE"
        session.writer_owner = WriterOwner(
            kind="orchestrator",
            transport_kind=transport_kind,
            actor_id=(tx.approved_by or tx.requested_by).actor_id,
            acquired_at=now,
        )
        session.generation = new_generation
        session.last_progress_at = now
        session.last_progress_event = "takeover.completed"
        blocked.state = "submitted"

        tx.phase = TakeoverPhase.COMPLETED
        tx.transport_kind = transport_kind
        tx.transport_ref = dict(transport_ref)
        tx.completed_at = now
        return tx

    def _require_takeover_session(self, session_id: str, generation: int) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise TakeoverError(BlockedReason.NOT_FOUND)
        if session.generation != generation:
            raise TakeoverError(BlockedReason.STALE_GENERATION)
        if not _session_is_external_tui_takeover_candidate(session):
            raise TakeoverError("session is not externally owned")
        return session

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessions": {key: _session_to_dict(value) for key, value in self._sessions.items()},
            "binding_to_session": {
                json.dumps(list(key)): value for key, value in self._binding_to_session.items()
            },
            "takeovers": {key: self._takeover_to_dict(value) for key, value in self._takeovers.items()},
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        now: Callable[[], float] = time.time,
    ) -> "SessionRegistry":
        # Files written before v0.14.36 also carry "lease_ttl" and "pending"
        # (the retired writer-lease and pending-binding bookkeeping): ignored.
        registry = cls(now=now)
        registry._sessions = {
            str(key): _session_from_dict(value)
            for key, value in data.get("sessions", {}).items()
            if isinstance(value, dict)
        }
        for raw_key, session_id in data.get("binding_to_session", {}).items():
            try:
                key = tuple(json.loads(raw_key))
            except Exception:
                continue
            if len(key) == 5:
                registry._binding_to_session[key] = str(session_id)  # type: ignore[index]
        if not registry._binding_to_session:
            registry._binding_to_session = {
                session.channel_binding.key(): session_id
                for session_id, session in registry._sessions.items()
                if session.channel_binding is not None
            }
        registry._takeovers = {
            str(key): cls._takeover_from_dict(value)
            for key, value in data.get("takeovers", {}).items()
            if isinstance(value, dict)
        }
        return registry

    @staticmethod
    def _takeover_to_dict(tx: TakeoverTransaction) -> dict[str, Any]:
        return {
            "takeover_id": tx.takeover_id,
            "session_id": tx.session_id,
            "blocked_input_id": tx.blocked_input_id,
            "requested_by": _actor_to_dict(tx.requested_by),
            "requested_generation": tx.requested_generation,
            "phase": tx.phase,
            "created_at": tx.created_at,
            "approved_by": _actor_to_dict(tx.approved_by),
            "resume_ref": dict(tx.resume_ref) if tx.resume_ref else None,
            "transport_kind": tx.transport_kind,
            "transport_ref": dict(tx.transport_ref),
            "authorized_at": tx.authorized_at,
            "completed_at": tx.completed_at,
            "reason": tx.reason,
        }

    @staticmethod
    def _takeover_from_dict(data: dict[str, Any]) -> TakeoverTransaction:
        requested_by = _actor_from_dict(data.get("requested_by")) or ActorRef("", "")
        return TakeoverTransaction(
            takeover_id=str(data.get("takeover_id", "")),
            session_id=str(data.get("session_id", "")),
            blocked_input_id=str(data.get("blocked_input_id", "")),
            requested_by=requested_by,
            requested_generation=int(data.get("requested_generation", 0)),
            phase=str(data.get("phase", TakeoverPhase.PROMPTED)),
            created_at=float(data.get("created_at", 0.0)),
            approved_by=_actor_from_dict(data.get("approved_by")),
            resume_ref=dict(data["resume_ref"]) if isinstance(data.get("resume_ref"), dict) else None,
            transport_kind=str(data.get("transport_kind", "")),
            transport_ref=dict(data.get("transport_ref", {})),
            authorized_at=data.get("authorized_at"),
            completed_at=data.get("completed_at"),
            reason=str(data.get("reason", "")),
        )


@dataclass
class InteractionContext:
    interaction_id: str
    session_id: str
    generation: int
    created_at: float
    expires_at: float
    tool_name: str
    tool_input: dict[str, Any]
    actions: list[str]
    transport_request_id: str = ""
    high_risk: bool = False
    kind: str = "permission"
    questions: list[dict[str, Any]] = field(default_factory=list)
    answers: dict[int, Any] = field(default_factory=dict)
    awaiting_other: dict[str, Any] | None = None
    decision: dict[str, Any] | None = None
    decided_by: ActorRef | None = None
    decided_at: float | None = None
    hitl_request_id: str = ""


@dataclass
class DecisionResult:
    accepted: bool
    reason: str = ""
    decision: dict[str, Any] | None = None


@dataclass
class CallbackToken:
    token: str
    interaction_id: str
    action: str
    generation: int
    expires_at: float


class InteractionStore:
    def __init__(
        self,
        *,
        now: Callable[[], float] = time.time,
        token_ttl: float = 600.0,
        decided_retention: float = 86400.0,
    ):
        self._now = now
        self._token_ttl = token_ttl
        self._decided_retention = decided_retention
        self._interactions: dict[str, InteractionContext] = {}
        self._tokens: dict[str, CallbackToken] = {}
        self._awaiting_other_by_binding: dict[BindingKey, str] = {}

    def register_permission(
        self,
        *,
        session_id: str,
        generation: int,
        tool_name: str,
        tool_input: dict[str, Any],
        actions: list[str],
        transport_request_id: str = "",
        high_risk: bool = False,
        hitl_request_id: str = "",
        ttl: float | None = None,
    ) -> InteractionContext:
        interaction_id = f"int-{uuid.uuid4().hex}"
        now = self._now()
        ctx = InteractionContext(
            interaction_id=interaction_id,
            session_id=session_id,
            generation=generation,
            created_at=now,
            # ttl override: gate prompts must stay decidable for the whole
            # hook wait window (default 30 min), not the 10-min card default.
            expires_at=now + (ttl if ttl and ttl > 0 else self._token_ttl),
            tool_name=tool_name,
            tool_input=dict(tool_input),
            actions=list(actions),
            transport_request_id=transport_request_id or interaction_id,
            high_risk=high_risk,
            hitl_request_id=hitl_request_id,
        )
        self._interactions[interaction_id] = ctx
        return ctx

    def register_ask_user_question(
        self,
        *,
        session_id: str,
        generation: int,
        questions: list[dict[str, Any]],
        transport_request_id: str = "",
        hitl_request_id: str = "",
        ttl: float | None = None,
    ) -> InteractionContext:
        interaction_id = f"int-{uuid.uuid4().hex}"
        now = self._now()
        ctx = InteractionContext(
            interaction_id=interaction_id,
            session_id=session_id,
            generation=generation,
            created_at=now,
            expires_at=now + (ttl if ttl and ttl > 0 else self._token_ttl),
            tool_name="",
            tool_input={},
            actions=[],
            transport_request_id=transport_request_id or interaction_id,
            kind="ask_user_question",
            questions=[dict(question) for question in questions],
            hitl_request_id=hitl_request_id,
        )
        self._interactions[interaction_id] = ctx
        return ctx

    def register_takeover(
        self,
        *,
        session_id: str,
        generation: int,
        takeover_id: str,
        blocked_input_id: str,
        actions: list[str] | None = None,
    ) -> InteractionContext:
        interaction_id = f"int-{uuid.uuid4().hex}"
        now = self._now()
        ctx = InteractionContext(
            interaction_id=interaction_id,
            session_id=session_id,
            generation=generation,
            created_at=now,
            expires_at=now + self._token_ttl,
            tool_name="",
            tool_input={
                "takeover_id": takeover_id,
                "blocked_input_id": blocked_input_id,
            },
            actions=list(actions or ["takeover_and_send"]),
            kind="takeover",
        )
        self._interactions[interaction_id] = ctx
        return ctx

    def register_model_choice(
        self,
        *,
        session_id: str,
        generation: int,
        models: list[dict[str, Any]],
        current: str = "",
    ) -> InteractionContext:
        interaction_id = f"int-{uuid.uuid4().hex}"
        now = self._now()
        ctx = InteractionContext(
            interaction_id=interaction_id,
            session_id=session_id,
            generation=generation,
            created_at=now,
            expires_at=now + self._token_ttl,
            tool_name="",
            tool_input={"models": [dict(m) for m in models], "current": current},
            actions=[str(m.get("slug", "")) for m in models if m.get("slug")],
            kind="model_choice",
        )
        self._interactions[interaction_id] = ctx
        return ctx

    def get(self, interaction_id: str) -> InteractionContext:
        return self._interactions[interaction_id]

    def create_callback_token(self, interaction_id: str, action: str, *, generation: int) -> str:
        token = uuid.uuid4().hex[:20]
        expires_at = self._now() + self._token_ttl
        ctx = self._interactions.get(interaction_id)
        if ctx is not None:
            # Tokens must not die before their interaction: gate prompts carry
            # an extended TTL matched to the blocking hook's wait window.
            expires_at = max(expires_at, ctx.expires_at)
        self._tokens[token] = CallbackToken(
            token=token,
            interaction_id=interaction_id,
            action=action,
            generation=generation,
            expires_at=expires_at,
        )
        return token

    def decide_from_token(
        self,
        token: str,
        *,
        actor: ActorRef,
        current_generation: int,
        binding_key: BindingKey | None = None,
    ) -> DecisionResult:
        token_state = self._tokens.get(token)
        if token_state is None or token_state.expires_at <= self._now():
            return DecisionResult(False, BlockedReason.INVALID_TOKEN)
        if token_state.generation != current_generation:
            return DecisionResult(False, BlockedReason.STALE_GENERATION)
        ctx = self._interactions.get(token_state.interaction_id)
        if ctx is None:
            return DecisionResult(False, BlockedReason.INVALID_TOKEN)
        if ctx.decision is None and ctx.expires_at <= self._now():
            return DecisionResult(False, BlockedReason.INVALID_TOKEN)
        if ctx.generation != current_generation:
            return DecisionResult(False, BlockedReason.STALE_GENERATION)
        if ctx.decision is not None:
            return DecisionResult(False, BlockedReason.ALREADY_DECIDED, ctx.decision)
        if ctx.kind == "ask_user_question":
            return self._decide_ask_user_question(
                ctx,
                token_state.action,
                actor=actor,
                binding_key=binding_key,
            )
        decision = {"action": token_state.action}
        ctx.decision = decision
        ctx.decided_by = actor
        ctx.decided_at = self._now()
        return DecisionResult(True, decision=decision)

    def context_for_token(self, token: str) -> InteractionContext | None:
        token_state = self._tokens.get(token)
        if token_state is None or token_state.expires_at <= self._now():
            return None
        return self._interactions.get(token_state.interaction_id)

    def awaiting_context_for_binding(self, binding_key: BindingKey) -> InteractionContext | None:
        interaction_id = self._awaiting_other_by_binding.get(binding_key)
        if interaction_id is None:
            return None
        ctx = self._interactions.get(interaction_id)
        if ctx is None or ctx.decision is not None or ctx.expires_at <= self._now():
            # A wait nobody can answer any more must not capture the next
            # plain message: it would be swallowed with no notice until the
            # compaction tick removed the mapping.
            if ctx is not None:
                ctx.awaiting_other = None
            self._awaiting_other_by_binding.pop(binding_key, None)
            return None
        return ctx

    def clear_awaiting_other_for_session(
        self,
        session_id: str,
        *,
        through_generation: int | None = None,
    ) -> int:
        """Retire free-text ("Other") waits for a session's old prompts.

        The handoff stale sweeps (ADR 0051) mark HitlRequests stale, but an
        AskUserQuestion that already entered awaiting-other keeps its
        binding mapping — later plain messages in the topic would be
        swallowed by the dead wait instead of reaching the new writer.
        """
        removed = 0
        for binding_key, interaction_id in list(self._awaiting_other_by_binding.items()):
            ctx = self._interactions.get(interaction_id)
            if ctx is None:
                self._awaiting_other_by_binding.pop(binding_key, None)
                removed += 1
                continue
            if ctx.session_id != session_id:
                continue
            if through_generation is not None and ctx.generation > through_generation:
                continue
            ctx.awaiting_other = None
            self._awaiting_other_by_binding.pop(binding_key, None)
            removed += 1
        return removed

    def begin_awaiting_other(
        self,
        interaction_id: str,
        binding_key: BindingKey,
        *,
        question_index: int,
    ) -> None:
        ctx = self._interactions[interaction_id]
        ctx.awaiting_other = {
            "binding_key": binding_key,
            "question_index": question_index,
            "started_at": self._now(),
        }
        self._awaiting_other_by_binding[binding_key] = interaction_id

    def answer_awaiting_other(
        self,
        binding_key: BindingKey,
        *,
        actor: ActorRef,
        text: str,
        current_generation: int,
    ) -> DecisionResult:
        interaction_id = self._awaiting_other_by_binding.get(binding_key)
        if interaction_id is None:
            return DecisionResult(False, BlockedReason.NOT_FOUND)
        ctx = self._interactions[interaction_id]
        if ctx.decision is not None:
            # The interaction settled while the awaiting-other mapping was
            # still around; drop the stale mapping so later plain messages go
            # to the agent instead of being swallowed as answers.
            self._awaiting_other_by_binding.pop(binding_key, None)
            return DecisionResult(False, BlockedReason.ALREADY_DECIDED, ctx.decision)
        if ctx.generation != current_generation:
            # A handoff bumped the generation: the wait can never be
            # answered again — drop the mapping so later plain messages
            # reach the new writer instead of hitting this dead wait.
            ctx.awaiting_other = None
            self._awaiting_other_by_binding.pop(binding_key, None)
            return DecisionResult(False, BlockedReason.STALE_GENERATION)
        if ctx.expires_at <= self._now():
            return DecisionResult(False, BlockedReason.INVALID_TOKEN)
        if not ctx.awaiting_other:
            return DecisionResult(False, BlockedReason.NOT_FOUND)
        question_index = int(ctx.awaiting_other["question_index"])
        if question_index < 0 or question_index >= len(ctx.questions):
            return DecisionResult(False, BlockedReason.INVALID_TOKEN)
        ctx.answers[question_index] = text
        ctx.awaiting_other = None
        self._awaiting_other_by_binding.pop(binding_key, None)
        # Free-text just fills that question; the user still submits the batch.
        return DecisionResult(
            True, decision={"action": "update", "question_index": question_index}
        )

    def interaction_count(self) -> int:
        return len(self._interactions)

    def token_count(self) -> int:
        return len(self._tokens)

    def undecided_session_ids(self) -> set[str]:
        return {ctx.session_id for ctx in self._interactions.values() if ctx.decision is None}

    def awaiting_other_count(self) -> int:
        return len(self._awaiting_other_by_binding)

    def compact(self) -> dict[str, int]:
        now = self._now()
        removed_interaction_ids: set[str] = set()
        for interaction_id, ctx in list(self._interactions.items()):
            if ctx.decision is not None:
                decided_at = ctx.decided_at if ctx.decided_at is not None else ctx.created_at
                if decided_at + self._decided_retention <= now:
                    removed_interaction_ids.add(interaction_id)
            elif ctx.expires_at <= now:
                removed_interaction_ids.add(interaction_id)

        removed_tokens = 0
        for token, token_state in list(self._tokens.items()):
            if token_state.expires_at <= now or token_state.interaction_id in removed_interaction_ids:
                self._tokens.pop(token, None)
                removed_tokens += 1

        for interaction_id in removed_interaction_ids:
            self._interactions.pop(interaction_id, None)

        removed_awaiting = 0
        for binding_key, interaction_id in list(self._awaiting_other_by_binding.items()):
            ctx = self._interactions.get(interaction_id)
            if ctx is None or ctx.awaiting_other is None:
                self._awaiting_other_by_binding.pop(binding_key, None)
                removed_awaiting += 1

        return {
            "interactions": len(removed_interaction_ids),
            "tokens": removed_tokens,
            "awaiting_other": removed_awaiting,
        }

    def _decide_ask_user_question(
        self,
        ctx: InteractionContext,
        action: str,
        *,
        actor: ActorRef,
        binding_key: BindingKey | None,
    ) -> DecisionResult:
        # Batch model: all questions on one card. set/toggle mutate a pending
        # answer and re-render (no finalize); submit_all commits everything.
        if action == "submit_all":
            return self._finalize_ask_user(ctx, actor)
        parts = action.split(":")
        if len(parts) < 2:
            return DecisionResult(False, BlockedReason.INVALID_TOKEN)
        command = parts[0]
        try:
            question_index = int(parts[1])
        except ValueError:
            return DecisionResult(False, BlockedReason.INVALID_TOKEN)
        if question_index < 0 or question_index >= len(ctx.questions):
            return DecisionResult(False, BlockedReason.INVALID_TOKEN)
        question = ctx.questions[question_index]

        if command in {"set", "answer"} and len(parts) == 3 and not question.get("allow_multiple"):
            try:
                option_index = int(parts[2])
            except ValueError:
                return DecisionResult(False, BlockedReason.INVALID_TOKEN)
            options = list(question.get("options", []))
            if option_index < 0 or option_index >= len(options):
                return DecisionResult(False, BlockedReason.INVALID_TOKEN)
            ctx.answers[question_index] = options[option_index]
            # "answer" = single simple question → finalize on click; "set" =
            # batch radio → just update, wait for Submit.
            if command == "answer":
                return self._finalize_ask_user(ctx, actor)
            return DecisionResult(True, decision={"action": "update"})

        if command == "toggle" and len(parts) == 3 and question.get("allow_multiple"):
            try:
                option_index = int(parts[2])
            except ValueError:
                return DecisionResult(False, BlockedReason.INVALID_TOKEN)
            options = list(question.get("options", []))
            if option_index < 0 or option_index >= len(options):
                return DecisionResult(False, BlockedReason.INVALID_TOKEN)
            selected = list(ctx.answers.get(question_index, []))
            value = options[option_index]
            if value in selected:
                selected.remove(value)
            else:
                selected.append(value)
            ctx.answers[question_index] = selected
            return DecisionResult(True, decision={"action": "update"})

        if command == "other" and question.get("allow_other"):
            if binding_key is None:
                return DecisionResult(False, BlockedReason.INVALID_TOKEN)
            self.begin_awaiting_other(ctx.interaction_id, binding_key, question_index=question_index)
            return DecisionResult(
                True,
                decision={"action": "awaiting_other", "question_index": question_index},
            )

        return DecisionResult(False, BlockedReason.INVALID_TOKEN)

    @staticmethod
    def _has_answer(value: Any) -> bool:
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, list):
            return len(value) > 0
        return value is not None

    def _finalize_ask_user(
        self,
        ctx: InteractionContext,
        actor: ActorRef,
    ) -> DecisionResult:
        # Refuse to settle a batch with unanswered questions — an empty or
        # partial answers dict would silently reach the agent as final input.
        missing = [
            index
            for index in range(len(ctx.questions))
            if not self._has_answer(ctx.answers.get(index))
        ]
        if missing:
            return DecisionResult(
                True, decision={"action": "incomplete", "missing": missing}
            )
        if ctx.awaiting_other:
            raw_key = ctx.awaiting_other.get("binding_key")
            if isinstance(raw_key, (list, tuple)) and len(raw_key) == 5:
                self._awaiting_other_by_binding.pop(tuple(raw_key), None)  # type: ignore[arg-type]
            ctx.awaiting_other = None
        decision = {"action": "answers", "answers": dict(ctx.answers)}
        ctx.decision = decision
        ctx.decided_by = actor
        ctx.decided_at = self._now()
        return DecisionResult(True, decision=decision)

    def apply_ask_user_form(
        self,
        token: str,
        form: dict[str, Any],
        *,
        current_generation: int,
    ) -> bool:
        """Write a Lark form_submit payload into the pending answers.

        Form fields: ``q{i}`` holds the picked option index (str) or index list
        for multi-select; ``q{i}_other`` holds free text that, when non-empty,
        overrides the picked options for that question. Returns False when the
        token doesn't resolve to an open ask_user interaction.
        """
        token_state = self._tokens.get(token)
        if token_state is None or token_state.expires_at <= self._now():
            return False
        ctx = self._interactions.get(token_state.interaction_id)
        if ctx is None or ctx.kind != "ask_user_question" or ctx.decision is not None:
            return False
        if ctx.generation != current_generation:
            return False
        for q_index, question in enumerate(ctx.questions):
            options = [str(option) for option in question.get("options", [])]
            other_text = str(form.get(f"q{q_index}_other", "") or "").strip()
            if other_text:
                ctx.answers[q_index] = other_text
                continue
            raw = form.get(f"q{q_index}")
            if raw is None or raw == "":
                continue
            if question.get("allow_multiple"):
                values = raw if isinstance(raw, list) else [raw]
                picked: list[str] = []
                for item in values:
                    try:
                        index = int(str(item))
                    except ValueError:
                        continue
                    if 0 <= index < len(options):
                        picked.append(options[index])
                if picked:
                    ctx.answers[q_index] = picked
            else:
                try:
                    index = int(str(raw))
                except (TypeError, ValueError):
                    continue
                if 0 <= index < len(options):
                    ctx.answers[q_index] = options[index]
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_ttl": self._token_ttl,
            "decided_retention": self._decided_retention,
            "interactions": {
                key: self._interaction_to_dict(value) for key, value in self._interactions.items()
            },
            "tokens": {key: self._token_to_dict(value) for key, value in self._tokens.items()},
            "awaiting_other_by_binding": [
                {"binding_key": list(key), "interaction_id": value}
                for key, value in self._awaiting_other_by_binding.items()
            ],
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        now: Callable[[], float] = time.time,
    ) -> "InteractionStore":
        store = cls(
            now=now,
            token_ttl=float(data.get("token_ttl", 600.0)),
            decided_retention=float(data.get("decided_retention", 86400.0)),
        )
        store._interactions = {
            str(key): cls._interaction_from_dict(value)
            for key, value in data.get("interactions", {}).items()
            if isinstance(value, dict)
        }
        store._tokens = {
            str(key): cls._token_from_dict(value)
            for key, value in data.get("tokens", {}).items()
            if isinstance(value, dict)
        }
        for item in data.get("awaiting_other_by_binding", []):
            if not isinstance(item, dict):
                continue
            raw_key = item.get("binding_key", [])
            if isinstance(raw_key, list) and len(raw_key) == 5:
                store._awaiting_other_by_binding[tuple(raw_key)] = str(item.get("interaction_id", ""))  # type: ignore[index]
        return store

    @staticmethod
    def _interaction_to_dict(ctx: InteractionContext) -> dict[str, Any]:
        return {
            "interaction_id": ctx.interaction_id,
            "session_id": ctx.session_id,
            "generation": ctx.generation,
            "created_at": ctx.created_at,
            "expires_at": ctx.expires_at,
            "tool_name": ctx.tool_name,
            "tool_input": dict(ctx.tool_input),
            "actions": list(ctx.actions),
            "transport_request_id": ctx.transport_request_id,
            "high_risk": ctx.high_risk,
            "kind": ctx.kind,
            "questions": [dict(question) for question in ctx.questions],
            "answers": {str(key): value for key, value in ctx.answers.items()},
            "awaiting_other": dict(ctx.awaiting_other) if ctx.awaiting_other else None,
            "decision": dict(ctx.decision) if ctx.decision else None,
            "decided_by": _actor_to_dict(ctx.decided_by),
            "decided_at": ctx.decided_at,
            "hitl_request_id": ctx.hitl_request_id,
        }

    @staticmethod
    def _interaction_from_dict(data: dict[str, Any]) -> InteractionContext:
        created_at = float(data.get("created_at", 0.0))
        interaction_id = str(data.get("interaction_id", ""))
        return InteractionContext(
            interaction_id=interaction_id,
            session_id=str(data.get("session_id", "")),
            generation=int(data.get("generation", 0)),
            created_at=created_at,
            expires_at=float(data.get("expires_at", created_at + 600.0)),
            tool_name=str(data.get("tool_name", "")),
            tool_input=dict(data.get("tool_input", {})),
            actions=[str(action) for action in data.get("actions", [])],
            transport_request_id=str(data.get("transport_request_id", "")) or interaction_id,
            high_risk=bool(data.get("high_risk", False)),
            kind=str(data.get("kind", "permission")),
            questions=[dict(question) for question in data.get("questions", []) if isinstance(question, dict)],
            answers={int(key): value for key, value in data.get("answers", {}).items()},
            awaiting_other=dict(data["awaiting_other"]) if isinstance(data.get("awaiting_other"), dict) else None,
            decision=dict(data["decision"]) if isinstance(data.get("decision"), dict) else None,
            decided_by=_actor_from_dict(data.get("decided_by")),
            decided_at=data.get("decided_at"),
            hitl_request_id=str(data.get("hitl_request_id", "")),
        )

    @staticmethod
    def _token_to_dict(token: CallbackToken) -> dict[str, Any]:
        return {
            "token": token.token,
            "interaction_id": token.interaction_id,
            "action": token.action,
            "generation": token.generation,
            "expires_at": token.expires_at,
        }

    @staticmethod
    def _token_from_dict(data: dict[str, Any]) -> CallbackToken:
        return CallbackToken(
            token=str(data.get("token", "")),
            interaction_id=str(data.get("interaction_id", "")),
            action=str(data.get("action", "")),
            generation=int(data.get("generation", 0)),
            expires_at=float(data.get("expires_at", 0.0)),
        )


@dataclass
class DeliveryItem:
    delivery_id: str
    seq: int
    channel_binding_key: BindingKey
    view_model: dict[str, Any]
    idempotency_key: str
    attempt_count: int = 0
    created_at: float = 0.0
    next_attempt_at: float = 0.0
    last_error: str = ""
    finished_at: float = 0.0
    claim_owner: str = ""
    claim_until: float = 0.0
    # Platform id of the sent message, so a card can be edited later without
    # a click (e.g. retiring a prompt the terminal answered instead).
    message_id: str = ""


def _sent_view_stub(view_model: dict[str, Any]) -> dict[str, Any]:
    """What a delivered item keeps of its view: only the type.

    Sent items stay for a day to dedupe by idempotency key and to remember the
    platform message id; the full card body was dead weight (about 1 MB of a
    busy instance's state file, rewritten with fsync on every save).
    """
    return {"type": str(view_model.get("type", "") or "")}


class DurableOutbox:
    def __init__(
        self,
        *,
        now: Callable[[], float] = time.time,
        max_attempts: int = 5,
        base_retry_delay: float = 1.0,
        sent_retention: float = 86400.0,
        dead_retention: float = 604800.0,
    ):
        self._now = now
        self._max_attempts = max_attempts
        self._base_retry_delay = base_retry_delay
        self._sent_retention = sent_retention
        self._dead_retention = dead_retention
        self._seq = 0
        self._pending: dict[str, DeliveryItem] = {}
        self._dead: dict[str, DeliveryItem] = {}
        self._sent: dict[str, DeliveryItem] = {}

    def enqueue(
        self,
        *,
        channel_binding_key: BindingKey,
        view_model: dict[str, Any],
        idempotency_key: str,
    ) -> DeliveryItem:
        existing = self._find_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing
        self._seq += 1
        item = DeliveryItem(
            delivery_id=f"del-{uuid.uuid4().hex}",
            seq=self._seq,
            channel_binding_key=channel_binding_key,
            view_model=dict(view_model),
            idempotency_key=idempotency_key,
            created_at=self._now(),
            next_attempt_at=self._now(),
        )
        self._pending[item.delivery_id] = item
        return item

    def _find_by_idempotency_key(self, idempotency_key: str) -> DeliveryItem | None:
        for bucket in (self._pending, self._sent, self._dead):
            for item in bucket.values():
                if item.idempotency_key == idempotency_key:
                    return item
        return None

    def sent_message_id(self, idempotency_key: str) -> str:
        item = self._find_by_idempotency_key(idempotency_key)
        return item.message_id if item is not None and item.delivery_id in self._sent else ""

    def is_pending(self, idempotency_key: str) -> bool:
        item = self._find_by_idempotency_key(idempotency_key)
        return item is not None and item.delivery_id in self._pending

    def get(self, delivery_id: str) -> DeliveryItem:
        if delivery_id in self._pending:
            return self._pending[delivery_id]
        if delivery_id in self._dead:
            return self._dead[delivery_id]
        return self._sent[delivery_id]

    def pending_count(self) -> int:
        return len(self._pending)

    def dead_count(self) -> int:
        return len(self._dead)

    def sent_count(self) -> int:
        return len(self._sent)

    def pending_binding_keys(self) -> set[BindingKey]:
        return {tuple(item.channel_binding_key) for item in self._pending.values()}  # type: ignore[misc]

    def pending_items(self) -> list[DeliveryItem]:
        now = self._now()
        return sorted(
            (
                item
                for item in self._pending.values()
                if item.next_attempt_at <= now and self._claim_is_available(item, now=now)
            ),
            key=lambda item: item.seq,
        )

    def claim_ready(
        self,
        *,
        owner: str,
        lease_ttl: float = 60.0,
        limit: int | None = None,
    ) -> list[DeliveryItem]:
        now = self._now()
        items = self.pending_items()
        if limit is not None:
            items = items[: max(0, int(limit))]
        claim_until = now + max(1.0, float(lease_ttl))
        for item in items:
            item.claim_owner = owner
            item.claim_until = claim_until
        return items

    @staticmethod
    def _claim_is_available(item: DeliveryItem, *, now: float) -> bool:
        return not item.claim_owner or item.claim_until <= now

    def record_result(
        self,
        delivery_id: str,
        status: str,
        error: str = "",
        *,
        claim_owner: str = "",
        retry_after: float | None = None,
        message_id: str = "",
    ) -> bool:
        item = self._pending.get(delivery_id)
        if item is None:
            return False
        if claim_owner and item.claim_owner and item.claim_owner != claim_owner:
            return False
        item.attempt_count += 1
        item.last_error = error
        if status == DeliveryStatus.SENT:
            item.finished_at = self._now()
            item.message_id = message_id
            item.view_model = _sent_view_stub(item.view_model)
            self._sent[delivery_id] = self._pending.pop(delivery_id)
            return True
        elif status == DeliveryStatus.PERMANENT_FAILURE:
            item.finished_at = self._now()
            self._dead[delivery_id] = self._pending.pop(delivery_id)
            return True
        elif status == DeliveryStatus.TRANSIENT_FAILURE:
            item.claim_owner = ""
            item.claim_until = 0.0
            if item.attempt_count >= self._max_attempts:
                item.finished_at = self._now()
                self._dead[delivery_id] = self._pending.pop(delivery_id)
                return True
            delay = self._base_retry_delay * (2 ** max(item.attempt_count - 1, 0))
            if retry_after is not None:
                delay = max(delay, max(0.0, float(retry_after)))
            item.next_attempt_at = self._now() + delay
            return True
        else:
            raise ValueError(f"unknown delivery status: {status}")

    def compact(self) -> dict[str, int]:
        now = self._now()
        removed_sent = self._compact_bucket(self._sent, now=now, retention=self._sent_retention)
        removed_dead = self._compact_bucket(self._dead, now=now, retention=self._dead_retention)
        return {"sent": removed_sent, "dead": removed_dead}

    @staticmethod
    def _compact_bucket(bucket: dict[str, DeliveryItem], *, now: float, retention: float) -> int:
        expired = []
        for delivery_id, item in bucket.items():
            finished_at = item.finished_at or item.created_at
            if finished_at + retention <= now:
                expired.append(delivery_id)
        for delivery_id in expired:
            bucket.pop(delivery_id, None)
        return len(expired)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self._seq,
            "max_attempts": self._max_attempts,
            "base_retry_delay": self._base_retry_delay,
            "sent_retention": self._sent_retention,
            "dead_retention": self._dead_retention,
            "pending": {key: _delivery_to_dict(value) for key, value in self._pending.items()},
            "dead": {key: _delivery_to_dict(value) for key, value in self._dead.items()},
            "sent": {key: _delivery_to_dict(value) for key, value in self._sent.items()},
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        now: Callable[[], float] = time.time,
    ) -> "DurableOutbox":
        outbox = cls(
            now=now,
            max_attempts=int(data.get("max_attempts", 5)),
            base_retry_delay=float(data.get("base_retry_delay", 1.0)),
            sent_retention=float(data.get("sent_retention", 86400.0)),
            dead_retention=float(data.get("dead_retention", 604800.0)),
        )
        outbox._seq = int(data.get("seq", 0))
        outbox._pending = {
            str(key): _delivery_from_dict(value) for key, value in data.get("pending", {}).items()
        }
        outbox._dead = {str(key): _delivery_from_dict(value) for key, value in data.get("dead", {}).items()}
        outbox._sent = {str(key): _delivery_from_dict(value) for key, value in data.get("sent", {}).items()}
        for item in outbox._sent.values():
            item.view_model = _sent_view_stub(item.view_model)
        return outbox


class InboundLedger:
    def __init__(self, *, now: Callable[[], float] = time.time, ttl: float = 3600.0):
        self._now = now
        self._ttl = ttl
        self._completed: dict[str, float] = {}
        self._in_progress: dict[str, float] = {}

    def record(self, event_id: str) -> bool:
        if not self.start(event_id):
            return False
        self.complete(event_id)
        return True

    def start(self, event_id: str) -> bool:
        now = self._now()
        self._expire(now)
        if event_id in self._completed or event_id in self._in_progress:
            return False
        self._in_progress[event_id] = now + self._ttl
        return True

    def seen(self, event_id: str) -> bool:
        """Read-only duplicate probe: True while the event is in progress or
        completed within the TTL. Unlike start(), never claims the event, so
        callers can screen redeliveries early and still let the real handler
        own the start/complete/fail lifecycle."""
        self._expire(self._now())
        return event_id in self._completed or event_id in self._in_progress

    def complete(self, event_id: str) -> None:
        self._in_progress.pop(event_id, None)
        self._completed[event_id] = self._now() + self._ttl

    def fail(self, event_id: str) -> None:
        self._in_progress.pop(event_id, None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ttl": self._ttl,
            "completed": dict(self._completed),
            "in_progress": dict(self._in_progress),
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        now: Callable[[], float] = time.time,
    ) -> "InboundLedger":
        ledger = cls(now=now, ttl=float(data.get("ttl", 3600.0)))
        ledger._completed = {str(key): float(value) for key, value in data.get("completed", {}).items()}
        ledger._in_progress = {str(key): float(value) for key, value in data.get("in_progress", {}).items()}
        return ledger

    def _expire(self, now: float) -> None:
        expired = [event_id for event_id, expires_at in self._completed.items() if expires_at <= now]
        for event_id in expired:
            self._completed.pop(event_id, None)
        expired_in_progress = [
            event_id for event_id, expires_at in self._in_progress.items() if expires_at <= now
        ]
        for event_id in expired_in_progress:
            self._in_progress.pop(event_id, None)


@dataclass
class HitlRequest:
    hitl_request_id: str
    session_id: str
    generation: int
    transport_kind: str
    transport_request_id: str
    native_method: str
    prompt_kind: str
    created_at: float
    expires_at: float
    status: str = "pending"
    # Read by the gate card retire path (v0.14.34) to close the matching card.
    interaction_id: str = ""
    # When the request was answered; the retention clock for "decided".
    decided_at: float = 0.0
    # PreToolUse-gate cards: still showing live buttons, and where. Persisted
    # so a card whose hook returned while the runtime was down (or whose edit
    # was still being retried) is retired after a restart.
    card_open: bool = False
    card_message_id: str = ""


# How long a gate card that could not be retired keeps its HITL record.
GATE_CARD_RETIRE_HORIZON_SECONDS = 7 * 86400.0


class HitlStore:
    def __init__(
        self,
        *,
        now: Callable[[], float] = time.time,
        request_ttl: float = 3600.0,
        decided_retention: float = 86400.0,
    ):
        self._now = now
        self._request_ttl = request_ttl
        self._decided_retention = decided_retention
        self._requests: dict[str, HitlRequest] = {}
        self._by_transport: dict[tuple[str, str, str], str] = {}

    def register_request(
        self,
        *,
        session_id: str,
        generation: int,
        transport_kind: str,
        transport_request_id: str,
        native_method: str,
        prompt_kind: str,
    ) -> HitlRequest:
        key = (session_id, transport_kind, transport_request_id)
        existing_id = self._by_transport.get(key)
        if existing_id:
            existing = self._requests.get(existing_id)
            if existing is not None and existing.status == "pending":
                return existing
        now = self._now()
        request = HitlRequest(
            hitl_request_id=f"hitl-{uuid.uuid4().hex}",
            session_id=session_id,
            generation=generation,
            transport_kind=transport_kind,
            transport_request_id=transport_request_id,
            native_method=native_method,
            prompt_kind=prompt_kind,
            created_at=now,
            expires_at=now + self._request_ttl,
            card_open=native_method == "pre_tool_use_hook",
        )
        self._requests[request.hitl_request_id] = request
        self._by_transport[key] = request.hitl_request_id
        return request

    def attach_interaction(self, hitl_request_id: str, interaction_id: str) -> None:
        request = self._requests.get(hitl_request_id)
        if request is not None:
            request.interaction_id = interaction_id

    def get(self, hitl_request_id: str) -> HitlRequest:
        return self._requests[hitl_request_id]

    def pending_session_ids(self) -> set[str]:
        return {request.session_id for request in self._requests.values() if request.status == "pending"}

    def pending_for_session(self, session_id: str) -> list[HitlRequest]:
        now = self._now()
        return [
            request
            for request in self._requests.values()
            if request.session_id == session_id
            and request.status == "pending"
            and request.expires_at > now
        ]

    def open_gate_cards(self) -> list[HitlRequest]:
        """Gate requests whose card still shows live buttons, whatever their
        status (a stale one may be mid-retire across a restart)."""
        return [request for request in self._requests.values() if request.card_open]

    def mark_decided(self, hitl_request_id: str) -> None:
        request = self._requests[hitl_request_id]
        request.status = "decided"
        request.decided_at = self._now()
        # The click that decided it flips the card itself.
        request.card_open = False

    def mark_pending_for_session_stale(
        self,
        session_id: str,
        *,
        through_generation: int | None = None,
    ) -> list[HitlRequest]:
        stale: list[HitlRequest] = []
        for request in self.pending_for_session(session_id):
            if through_generation is not None and request.generation > through_generation:
                continue
            request.status = "stale"
            stale.append(request)
        return stale

    def compact(self) -> dict[str, int]:
        now = self._now()
        removed_requests = 0
        for hitl_id, request in list(self._requests.items()):
            if request.status == "pending" and request.expires_at <= now:
                request.status = "expired"
            if request.card_open and request.created_at + GATE_CARD_RETIRE_HORIZON_SECONDS > now:
                # Its card still needs retiring; keep what the edit needs.
                continue
            if request.status in {"decided", "stale", "expired"}:
                # Requests decided before v0.14.36 carry no decided_at; they
                # were answered before expiring, so expires_at is a later,
                # still bounded, reference.
                reference_time = request.decided_at or request.expires_at
                if reference_time + self._decided_retention <= now:
                    self._requests.pop(hitl_id, None)
                    self._by_transport.pop(
                        (request.session_id, request.transport_kind, request.transport_request_id),
                        None,
                    )
                    removed_requests += 1
        return {"requests": removed_requests}

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_ttl": self._request_ttl,
            "decided_retention": self._decided_retention,
            "requests": {
                hitl_id: self._request_to_dict(request)
                for hitl_id, request in self._requests.items()
            },
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        now: Callable[[], float] = time.time,
    ) -> "HitlStore":
        store = cls(
            now=now,
            request_ttl=float(data.get("request_ttl", 3600.0)),
            decided_retention=float(data.get("decided_retention", 86400.0)),
        )
        store._requests = {
            str(hitl_id): cls._request_from_dict(value)
            for hitl_id, value in data.get("requests", {}).items()
            if isinstance(value, dict)
        }
        # Files written before v0.14.36 also carry "decisions" (a write-only
        # copy of each answer) and per-request native_params /
        # channel_binding_key: ignored.
        for hitl_id, request in store._requests.items():
            store._by_transport[
                (request.session_id, request.transport_kind, request.transport_request_id)
            ] = hitl_id
        return store

    @staticmethod
    def _request_to_dict(request: HitlRequest) -> dict[str, Any]:
        return {
            "hitl_request_id": request.hitl_request_id,
            "session_id": request.session_id,
            "generation": request.generation,
            "transport_kind": request.transport_kind,
            "transport_request_id": request.transport_request_id,
            "native_method": request.native_method,
            "prompt_kind": request.prompt_kind,
            "created_at": request.created_at,
            "expires_at": request.expires_at,
            "status": request.status,
            "interaction_id": request.interaction_id,
            "decided_at": request.decided_at,
            "card_open": request.card_open,
            "card_message_id": request.card_message_id,
        }

    @staticmethod
    def _request_from_dict(data: dict[str, Any]) -> HitlRequest:
        return HitlRequest(
            hitl_request_id=str(data.get("hitl_request_id", "")),
            session_id=str(data.get("session_id", "")),
            generation=int(data.get("generation", 0)),
            transport_kind=str(data.get("transport_kind", "")),
            transport_request_id=str(data.get("transport_request_id", "")),
            native_method=str(data.get("native_method", "")),
            prompt_kind=str(data.get("prompt_kind", "")),
            created_at=float(data.get("created_at", 0.0)),
            expires_at=float(data.get("expires_at", 0.0)),
            status=str(data.get("status", "pending")),
            interaction_id=str(data.get("interaction_id", "")),
            decided_at=float(data.get("decided_at", 0.0) or 0.0),
            # Pre-v0.14.39 records carry no flag: a gate request still pending
            # then has a live card that needs retiring.
            card_open=bool(
                data["card_open"]
                if "card_open" in data
                else data.get("native_method") == "pre_tool_use_hook" and data.get("status", "pending") == "pending"
            ),
            card_message_id=str(data.get("card_message_id", "") or ""),
        )


@dataclass
class StateSnapshot:
    sessions: SessionRegistry
    interactions: InteractionStore
    outbox: DurableOutbox
    authz: AuthorizationStore
    inbound_ledger: InboundLedger
    hitls: HitlStore = field(default_factory=HitlStore)


def compact_sessions(state: StateSnapshot) -> dict[str, int]:
    """Session retention pass; run after the other stores' compaction.

    A session stays while anything still points at it: an undelivered outbox
    item for one of its bindings, an undecided interaction, or a pending HITL
    request. Its grants go with it.
    """
    referenced = state.interactions.undecided_session_ids() | state.hitls.pending_session_ids()
    pending_keys = state.outbox.pending_binding_keys()
    if pending_keys:
        referenced |= {
            session_id
            for session_id, keys in state.sessions.binding_keys_by_session().items()
            if keys & pending_keys
        }
    removed = state.sessions.prune_stopped_sessions(referenced=referenced)
    state.authz.drop_sessions(removed)
    return {"sessions": len(removed)}


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Replace a private JSON file only after its complete contents reach disk."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(path.parent),
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as tmp:
            tmp_name = tmp.name
            json.dump(payload, tmp, sort_keys=True, ensure_ascii=False)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    finally:
        if tmp_name is not None:
            Path(tmp_name).unlink(missing_ok=True)


# A temp file older than this next to the state file cannot belong to a
# writer still in progress (a full save takes milliseconds).
STALE_STATE_TEMP_SECONDS = 60.0


class JsonFileStateStore:
    def __init__(self, path: str | Path, *, now: Callable[[], float] = time.time):
        self.path = Path(path).expanduser()
        self._now = now
        # True while the in-memory snapshot holds changes the last save
        # attempt failed to persist. Callers that must not act on unsaved
        # state (dropping a replayable queue file) check it.
        self.last_save_failed = False

    def save(self, snapshot: StateSnapshot) -> None:
        # Whole snapshots only: saving from loose components let a caller
        # omit one (the debug repair commands dropped hitls) and silently
        # persist it as empty.
        try:
            payload = {
                "schema_version": 1,
                "sessions": snapshot.sessions.to_dict(),
                "interactions": snapshot.interactions.to_dict(),
                "outbox": snapshot.outbox.to_dict(),
                "authz": snapshot.authz.to_dict(),
                "inbound_ledger": snapshot.inbound_ledger.to_dict(),
                "hitls": snapshot.hitls.to_dict(),
            }
            _atomic_write_json(self.path, payload)
        except BaseException:
            self.last_save_failed = True
            raise
        self.last_save_failed = False

    def sweep_stale_temp_files(self, *, min_age: float = STALE_STATE_TEMP_SECONDS) -> int:
        """Delete temp files a killed writer left next to the state file.

        ``_atomic_write_json`` removes its temp file on any exception, but a
        SIGKILL mid-write skips that cleanup. Only files older than
        ``min_age`` (wall clock, like mtime) are removed, so another
        process's in-flight save is never touched.
        """
        cutoff = time.time() - min_age
        removed = 0
        for path in self.path.parent.glob(f".{self.path.name}.*.tmp"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except FileNotFoundError:
                continue
            except OSError as exc:
                _log_degrade("state_temp_sweep_failed", path=str(path), error=exc)
        return removed

    def load(self) -> StateSnapshot:
        with self.path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        return StateSnapshot(
            sessions=SessionRegistry.from_dict(payload.get("sessions", {}), now=self._now),
            interactions=InteractionStore.from_dict(payload.get("interactions", {}), now=self._now),
            outbox=DurableOutbox.from_dict(payload.get("outbox", {}), now=self._now),
            authz=AuthorizationStore.from_dict(payload.get("authz", {})),
            inbound_ledger=InboundLedger.from_dict(payload.get("inbound_ledger", {}), now=self._now),
            hitls=HitlStore.from_dict(payload.get("hitls", {}), now=self._now),
        )
