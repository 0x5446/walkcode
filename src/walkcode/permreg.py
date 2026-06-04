"""Permission-request state machine.

All state for one permission request lives in a single ``PermissionRequest``;
``PermissionRegistry`` owns the collection behind ONE re-entrant lock. Every
state transition is atomic and IO-free — callers perform lark/tmux IO only AFTER
a method returns, never while holding the lock.

Why this exists (it replaces six parallel module-level dicts in server.py):

* **Decision tearing (A).** The decision must be written exactly once. codex
  0.135 double-fires PreToolUse, and a Feishu card can be double-clicked or
  re-delivered, so two threads can race to set a verdict; the old code let the
  second overwrite the first (allow→deny tearing). ``set_decision_once`` makes the
  first writer win and reports refusal to the rest.
* **Fallback atomicity (B).** ``try_consume`` (the hook long-poller) and
  ``claim_fallback`` (the tmux backstop) are mutually exclusive, so a decision is
  never both delivered to the hook AND injected into the terminal.
* **Card-send retry (D).** ``card_sent`` / ``card_failed`` + ``await_send_result``
  let codex's duplicate reuse a sent card or take over sending if the first send
  failed — without stranding either hook polling a card nobody can see.

The card status / send_done machinery is also future-proofing: today the server's
lark IO is synchronous and blocks the single asyncio loop, so two ``/hook/permission``
requests can't actually overlap in the send section (the duplicate already sees a
terminal card status). Keeping ``await_send_result`` means the protocol still holds
if that IO is ever made async.
"""

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

# Backstop for a codex rid whose hook PROCESS died: reaped only after no poll for
# this long. Must exceed one poll cycle (hook waits 30s then sleeps 2s ≈ 32s) so a
# slow user whose hooks are still polling is never reaped.
_PERM_DEDUPE_TTL = 90.0
# Keep a consumed decision this long for a second poller (codex double-fire).
_PERM_GC_GRACE = 5.0
# The tmux fallback only claims a request that has had no poll for this long, so
# an actively-polling hook is never raced by the backstop.
_FALLBACK_QUIESCE = 5.0


class CardStatus(Enum):
    SENDING = "sending"
    READY = "ready"
    FAILED = "failed"


@dataclass
class PermissionRequest:
    """One permission request. Request-side fields (tool_name … feishu_root_msg_id)
    are set once at registration before any concurrency and may be read lock-free;
    every mutable field below is only touched through PermissionRegistry methods."""

    rid: str
    dedupe_key: tuple | None
    created_at: float
    # request side (set once right after registration)
    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    tty: str = ""
    hook_data_full: dict = field(default_factory=dict)
    permission_suggestions: list = field(default_factory=list)
    feishu_root_msg_id: str = ""
    # decision side — `decision` is the final, write-once verdict
    decision: dict | None = None
    # AskUserQuestion accumulation (mutable mid-flight, distinct from `decision`)
    answers: list = field(default_factory=list)
    pending_selections: dict = field(default_factory=dict)
    awaiting_other: dict | None = None
    # lifecycle
    decided: threading.Event = field(default_factory=threading.Event)
    consumed_at: float | None = None
    last_poll: float = 0.0
    fallback_claimed: bool = False
    # card delivery
    card_status: CardStatus = CardStatus.SENDING
    send_done: threading.Event = field(default_factory=threading.Event)

    def snapshot(self) -> dict:
        """Plain-dict copy of the request-side fields for IO callers
        (``_tmux_fallback`` reads tty / tool_name / permission_suggestions /
        hook_data_full)."""
        return {
            "rid": self.rid,
            "dedupe_key": self.dedupe_key,
            "tool_name": self.tool_name,
            "tool_input": dict(self.tool_input),
            "tty": self.tty,
            "hook_data_full": dict(self.hook_data_full),
            "permission_suggestions": list(self.permission_suggestions),
            "feishu_root_msg_id": self.feishu_root_msg_id,
        }


class PermissionRegistry:
    def __init__(self, *, ttl: float = _PERM_DEDUPE_TTL, grace: float = _PERM_GC_GRACE,
                 quiesce: float = _FALLBACK_QUIESCE,
                 now: Callable[[], float] = time.time):
        self._lock = threading.RLock()  # re-entrant: register_or_get/try_consume call _gc_locked
        self._by_rid: dict[str, PermissionRequest] = {}
        self._dedupe: dict[tuple, str] = {}  # dedupe_key → rid
        self._ttl = ttl
        self._grace = grace
        self._quiesce = quiesce
        self._now = now

    # --- internal ---------------------------------------------------------
    def _drop_locked(self, rid: str) -> None:
        """Remove a request and its dedupe entry — but only unmap the key if it
        still points at THIS rid (a duplicate may have re-claimed it after a
        card_failed released it)."""
        req = self._by_rid.pop(rid, None)
        if req is not None and req.dedupe_key is not None and self._dedupe.get(req.dedupe_key) == rid:
            self._dedupe.pop(req.dedupe_key, None)

    def _gc_locked(self) -> None:
        now = self._now()
        dead = []
        for rid, req in self._by_rid.items():
            if req.consumed_at is not None and now - req.consumed_at > self._grace:
                dead.append(rid)  # decision read; keep GRACE for a 2nd poller, then drop
            elif (req.dedupe_key is not None and req.decision is None
                  and now - max(req.created_at, req.last_poll) > self._ttl):
                # No decision AND no poll for a full TTL → the hook process is gone,
                # not a slow user (whose polls keep refreshing last_poll). A
                # None-key request (AskUserQuestion) is exempt — it may wait minutes.
                dead.append(rid)
        for rid in dead:
            self._drop_locked(rid)
        live = set(self._by_rid)
        for k in [k for k, r in self._dedupe.items() if r not in live]:
            del self._dedupe[k]

    # --- registration -----------------------------------------------------
    def register_or_get(self, key: tuple | None) -> tuple[PermissionRequest, bool]:
        """Return (request, is_new). key=None always builds a fresh request
        (AskUserQuestion / Claude, never deduped). Otherwise gc first, then if a
        live non-FAILED request already holds this key return it (is_new=False);
        else build a new request, reserve the key, card_status=SENDING."""
        now = self._now()
        with self._lock:
            if key is not None:
                self._gc_locked()
                existing_rid = self._dedupe.get(key)
                existing = self._by_rid.get(existing_rid) if existing_rid else None
                if existing is not None and existing.card_status is not CardStatus.FAILED:
                    return existing, False
            rid = str(uuid.uuid4())
            req = PermissionRequest(rid=rid, dedupe_key=key, created_at=now)
            self._by_rid[rid] = req
            if key is not None:
                self._dedupe[key] = rid
            return req, True

    def get(self, rid: str) -> PermissionRequest | None:
        with self._lock:
            return self._by_rid.get(rid)

    def fill_request(self, rid: str, **fields) -> None:
        """Set request-side fields right after registration (before concurrency)."""
        with self._lock:
            req = self._by_rid.get(rid)
            if req is None:
                return
            for k, v in fields.items():
                setattr(req, k, v)

    def remove(self, rid: str) -> None:
        with self._lock:
            self._drop_locked(rid)

    def gc(self) -> None:
        with self._lock:
            self._gc_locked()

    # --- decision: write-once (A) ----------------------------------------
    def set_decision_once(self, rid: str, decision: dict) -> bool:
        """Write the final decision iff none exists yet. First writer wins and gets
        True (and wakes pollers via `decided`); later writers — a double card click
        or codex double-fire — get False and MUST NOT overwrite."""
        with self._lock:
            req = self._by_rid.get(rid)
            if req is None or req.decision is not None:
                return False
            req.decision = decision
            req.decided.set()
            return True

    # --- consume vs fallback: mutually exclusive (B) ---------------------
    def try_consume(self, rid: str) -> dict | None:
        """Hook-poller path. Return the decision iff present and not claimed by the
        tmux fallback; stamp consumed_at (first reader) and refresh last_poll, then
        gc. None if there is no decision / the fallback already claimed it / unknown
        rid. Mutually exclusive with claim_fallback."""
        now = self._now()
        with self._lock:
            req = self._by_rid.get(rid)
            if req is None or req.fallback_claimed or req.decision is None:
                return None
            if req.consumed_at is None:
                req.consumed_at = now
            req.last_poll = now
            self._gc_locked()
            return req.decision

    def claim_fallback(self, rid: str) -> bool:
        """tmux-backstop path. Claim the right to inject iff no poller consumed the
        decision and the request has been quiet (no poll) for QUIESCE seconds.
        Mutually exclusive with try_consume."""
        now = self._now()
        with self._lock:
            req = self._by_rid.get(rid)
            if req is None or req.consumed_at is not None or req.fallback_claimed:
                return False
            if now - max(req.created_at, req.last_poll) < self._quiesce:
                return False
            req.fallback_claimed = True
            return True

    def mark_poll(self, rid: str) -> None:
        now = self._now()
        with self._lock:
            req = self._by_rid.get(rid)
            if req is not None:
                req.last_poll = now

    # --- card delivery (D) -----------------------------------------------
    def card_sent(self, rid: str) -> None:
        with self._lock:
            req = self._by_rid.get(rid)
            if req is not None:
                req.card_status = CardStatus.READY
                req.send_done.set()

    def card_failed(self, rid: str) -> None:
        """The card never reached Feishu. Mark FAILED and release the dedupe key so
        a duplicate re-registers as the new sender; keep the rid (status readable
        via await_send_result), and wake send-waiters + any poller. The request is
        reaped later by TTL gc (its dedupe_key stays set on the object for that)."""
        with self._lock:
            req = self._by_rid.get(rid)
            if req is None:
                return
            req.card_status = CardStatus.FAILED
            if req.dedupe_key is not None and self._dedupe.get(req.dedupe_key) == rid:
                self._dedupe.pop(req.dedupe_key, None)
            req.send_done.set()
            req.decided.set()

    def await_send_result(self, rid: str, timeout: float = 8.0) -> CardStatus:
        """Block until the first sender reports card_sent/card_failed (or timeout),
        then return the card status. A deduped duplicate uses this to decide whether
        to reuse the card (READY) or take over sending (FAILED). With synchronous
        lark IO this returns immediately because the first sender already finished."""
        req = self.get(rid)
        if req is None:
            return CardStatus.FAILED
        req.send_done.wait(timeout)
        with self._lock:
            req = self._by_rid.get(rid)
            return req.card_status if req is not None else CardStatus.FAILED

    # --- AskUserQuestion mid-flight accumulation -------------------------
    def askuser_toggle(self, rid: str, qi: int, option_idx: int) -> list | None:
        """multiSelect: toggle option_idx in pending_selections[qi]; return a copy
        of the new selection, or None if the rid is gone."""
        with self._lock:
            req = self._by_rid.get(rid)
            if req is None:
                return None
            selected = req.pending_selections.setdefault(qi, [])
            if option_idx in selected:
                selected.remove(option_idx)
            else:
                selected.append(option_idx)
            return list(selected)

    def askuser_get_selected(self, rid: str, qi: int) -> list:
        with self._lock:
            req = self._by_rid.get(rid)
            return list(req.pending_selections.get(qi, [])) if req is not None else []

    def askuser_set_awaiting_other(self, rid: str, qi: int, root_msg_id: str) -> bool:
        with self._lock:
            req = self._by_rid.get(rid)
            if req is None:
                return False
            req.awaiting_other = {"question_index": qi, "root_msg_id": root_msg_id}
            return True

    def askuser_record_answer(self, rid: str, qi: int, answer) -> list | None:
        """Record the answer for question qi (extending the list as needed) and
        clear any awaiting_other marker. Return a copy of the answers list, or None
        if the rid is gone. The caller decides has_next and, on the last question,
        builds the final decision and calls set_decision_once."""
        with self._lock:
            req = self._by_rid.get(rid)
            if req is None:
                return None
            while len(req.answers) <= qi:
                req.answers.append(None)
            req.answers[qi] = answer
            req.awaiting_other = None
            return list(req.answers)

    def find_awaiting_other(self, thread_root: str | None) -> str | None:
        """Return the rid whose AskUserQuestion is awaiting an Other thread-reply in
        the given Feishu thread root, or None."""
        if not thread_root:
            return None
        with self._lock:
            for rid, req in self._by_rid.items():
                if (req.tool_name == "AskUserQuestion"
                        and req.feishu_root_msg_id == thread_root
                        and req.awaiting_other):
                    return rid
        return None
