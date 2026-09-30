"""Codex app-server transport and the codex event mapping helpers."""

from __future__ import annotations

import asyncio
import re
import time
import uuid

from collections.abc import Callable
from typing import Any, NamedTuple

from .models import (
    AgentEvent,
    AgentEventType,
    CapabilityUnsupported,
    _compact_tool_summary,
    ControlResult,
    _humanize_seconds,
    LaunchSpec,
    _log_degrade,
    _maybe_await,
    ResumeSpec,
    _TOOL_SUMMARY_LIMIT,
    TransportCapabilities,
    TransportHandle,
    TurnInput,
    UnsafeSandboxError,
)
from .config import _CODEX_SANDBOX_POLICY_TYPES
from .claude_headless import _compose_turn_text, EMPTY_TURN_PLACEHOLDER


UNSAFE_SANDBOX_MESSAGE = (
    "refusing to run an unsandboxed Codex thread on a channel with no sender "
    "allowlist: anyone who can message this bot would get arbitrary command "
    "execution on this host. Set the channel's allowlist "
    "(LARK_ALLOWED_CHAT_IDS / LARK_ALLOWED_OPEN_IDS), or constrain the sandbox via "
    "WALKCODE_CODEX_SANDBOX, or opt in explicitly with "
    "WALKCODE_CODEX_ALLOW_UNRESTRICTED_WITHOUT_ALLOWLIST=1"
)


def _codex_message_turn_id(message: Any) -> str:
    """Turn id of an app-server message (notification or server request).

    Turn-scoped v2 messages carry ``params.turnId`` (``params.turn.id`` on
    turn/started and turn/completed); thread- and account-level ones carry
    none and belong to no turn. The legacy ``event_msg`` shape, which this
    transport also converts, keeps it in ``payload.turn_id`` — read there too,
    the same place _notification_thread_id finds its thread id.
    """
    if not isinstance(message, dict):
        return ""
    params = message.get("params")
    if isinstance(params, dict):
        turn = params.get("turn")
        turn_id = params.get("turnId") or (turn.get("id") if isinstance(turn, dict) else "")
        if turn_id:
            return str(turn_id)
    payload = message.get("payload")
    if isinstance(payload, dict):
        return str(payload.get("turn_id") or payload.get("turnId") or "")
    return ""


# How many started turn ids CodexAppServerTransport remembers for hook
# ownership. Only turns whose hooks are still queued matter, so a few hundred
# is generous; the cap keeps a long-lived runtime from growing without bound.
CODEX_STARTED_TURNS_LIMIT = 1024


class CodexAppServerTransport:
    kind = "codex_app_server"
    _HITL_SERVER_REQUEST_METHODS = {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
        "item/tool/requestUserInput",
        "mcpServer/elicitation/request",
    }
    # Events that hand the turn to a human. The collector returns early on
    # these by design, and the answer arrives through a separate call — so the
    # listen must stop WITHOUT closing the turn (it is parked, not finished).
    _TURN_PARKING_EVENTS = frozenset(
        {AgentEventType.PERMISSION_REQUESTED, AgentEventType.ASK_USER_REQUESTED}
    )
    # Floor between two empty collector batches — anti-spin only.
    _EMPTY_BATCH_MIN_INTERVAL = 1.0

    def __init__(
        self,
        *,
        client: Any,
        approval_policy: str = "never",
        sandbox_override: str | None = None,
        allowlist_configured: bool = True,
        unrestricted_without_allowlist_ok: bool = False,
        ephemeral: bool = False,
        environment_context: str = "",
        event_silence_ceiling: float = 3600.0,
    ):
        self.client = client
        self.approval_policy = approval_policy
        # None means: send no `sandbox` in thread/start or thread/resume and let
        # Codex apply the profile's own `sandbox_mode`. This used to default to
        # "read-only", which SILENTLY overrode a profile configured for
        # danger-full-access — every channel-launched thread lost network and
        # write access, and the model reported it as the whole machine being
        # locked down rather than as a walkcode setting. Deciding the sandbox is
        # the codex profile's job; walkcode only overrides when explicitly told
        # to via WALKCODE_CODEX_SANDBOX.
        #
        # This is an OVERRIDE, not the effective policy. The effective policy is
        # whatever the app-server echoes back in the thread/start response —
        # read it from `self.effective_sandbox`, never from this field.
        self.sandbox_override = sandbox_override
        # Whether the channel restricts who may talk to this bot. A thread that
        # ends up unsandboxed while ANY sender can reach it is remote arbitrary
        # command execution, so `_apply_effective_sandbox` refuses to run in
        # that combination unless it was opted into explicitly.
        self.allowlist_configured = allowlist_configured
        self.unrestricted_without_allowlist_ok = unrestricted_without_allowlist_ok
        # Effective policy echoed by the app-server, per thread id.
        self.effective_sandbox: dict[str, str] = {}
        self.ephemeral = ephemeral
        # Total silence for this long ends the listen (matches the Claude
        # background-wait ceiling). This is NOT the per-batch collector
        # timeout: batches roll over transparently while the turn is alive.
        self.event_silence_ceiling = event_silence_ceiling
        self._pending_server_requests: dict[str, dict[str, Any]] = {}
        # Codex app-server has no append-system-prompt surface
        # (base_instructions REPLACES the built-in prompt wholesale), so the
        # channel context rides the FIRST turn of each launched/resumed
        # thread instead. Pending until a turn/start confirms; a runtime
        # restart re-marks on the next resume — a repeated preamble in
        # history is harmless, a missing one is not.
        self.environment_context = environment_context
        self._env_context_pending: set[str] = set()
        # Threads that already received the context this runtime lifetime:
        # writer reacquisition resumes the same thread over and over, and
        # without this mark every resume would re-inject the preamble.
        self._env_context_delivered: set[str] = set()
        # Last model seen per thread. The app-server event stream carries the
        # model on thread_settings_applied / turn_context event_msg records,
        # NOT on agent_message / task_complete — so /status showed 模型: — for
        # every channel-launched codex session. We cache it here and backfill
        # it onto the converted events; _record_session_progress then writes
        # session.model from event.payload["model"].
        self._thread_models: dict[str, str] = {}
        # Live turn per thread, from the turn/start response. turn/interrupt
        # needs it, and without it an explicit close could only stop WalkCode
        # from listening while the agent kept running server-side.
        self._active_turns: dict[str, str] = {}
        # Every turn this transport started, kept past turn completion (the
        # Stop hook lands around the same moment and is drained later). The
        # runtime asks started_turn() to tell our own hooks from a TUI's:
        # under a shared app-server daemon both run hooks from the same
        # process, so the turn id is the only reliable owner mark. Bounded;
        # insertion order makes the oldest entry the first key.
        self._started_turns: dict[str, None] = {}
        # Threads shut down by close_session. The event loop checks this at
        # each batch boundary so a drain parked in client.events() ends with
        # the session instead of hanging around until the silence ceiling.
        self._released_threads: set[str] = set()
        # Codex event types we drop on the floor, logged once each. See
        # _log_unhandled_event_type.
        self._logged_unhandled_types: set[str] = set()
        # ADR 0065: another client (the TUI) can run turns on a thread we share
        # through the codex daemon. Per thread: where its turns go, and the
        # client connection generation the registration belongs to. Direct
        # routing only starts once the handover of already-queued messages is
        # done (thread in _foreign_direct), so one turn never arrives out of
        # order across the two paths.
        self._foreign_sinks: dict[str, tuple[Callable[[str, dict[str, Any]], None], int]] = {}
        self._foreign_direct: set[str] = set()
        # Threads with a turn/start in flight: a new turn id seen meanwhile
        # may be ours, so it stays on the queue path ("parked") until the
        # submit resolves and the parked messages are handed over in order.
        self._pending_starts: set[str] = set()
        self._parked_turns: dict[str, set[str]] = {}
        # Threads with a drain (events()) running: between two bounded client
        # reads the client sees no listener, but the drain may still be
        # handing over a batch it holds — the queue is still the drain's.
        self._draining: dict[str, int] = {}
        if hasattr(client, "foreign_router"):
            client.foreign_router = self._route_foreign_message

    def started_turn(self, turn_id: str) -> bool:
        """Did this transport start ``turn_id`` (recently enough to remember)?"""
        return bool(turn_id) and turn_id in self._started_turns

    def _client_generation(self) -> int:
        return int(getattr(self.client, "connection_generation", 0) or 0)

    def foreign_sink_current(self, thread_id: str) -> bool:
        """Is a mirror registered for this thread on the current connection?"""
        entry = self._foreign_sinks.get(thread_id)
        return entry is not None and entry[1] == self._client_generation()

    async def subscribe_foreign_mirror(
        self, thread_id: str, *, cwd: str, sink: Callable[[str, dict[str, Any]], None]
    ) -> None:
        """Subscribe this connection to ``thread_id`` and route others' turns to ``sink``.

        Only ``thread/resume`` — none of ``resume_thread``'s turn bookkeeping
        (environment context, release marks): this is a watcher, not a submit.
        While the request is out, everything keeps queueing; once it returns,
        ``_hand_over_queued_foreign`` passes the queued foreign-turn messages
        on in order and switches direct routing on (if a drain is listening,
        that happens when it ends). Idempotent per thread; a new connection
        generation re-subscribes.
        """
        self._foreign_direct.discard(thread_id)
        self._foreign_sinks[thread_id] = (sink, self._client_generation())
        params = self._with_sandbox_override({"threadId": thread_id, "cwd": cwd})
        try:
            await self.client.request("thread/resume", params)
        except BaseException:
            self._foreign_sinks.pop(thread_id, None)
            raise
        self._hand_over_queued_foreign(thread_id)

    def unsubscribe_foreign_mirror(self, thread_id: str) -> None:
        self._foreign_sinks.pop(thread_id, None)
        self._foreign_direct.discard(thread_id)
        self._parked_turns.pop(thread_id, None)

    def _foreign_verdict(self, thread_id: str, turn_id: str) -> str:
        """Who a turn-scoped message on a mirrored thread belongs to.

        ``ours``: a turn we started (or no turn id — thread-level, drain's).
        ``defer``: undecidable now (a turn/start of ours is in flight, or the
        turn is already parked) — stays on the queue path, parked.
        ``foreign``: another client's turn — the mirror's.
        """
        if not turn_id or self.started_turn(turn_id):
            return "ours"
        if thread_id in self._pending_starts or turn_id in self._parked_turns.get(thread_id, ()):
            return "defer"
        return "foreign"

    def _route_foreign_message(self, thread_id: str, message: dict[str, Any]) -> bool:
        """Client dispatch hook: take another client's turn off the queue path."""
        entry = self._foreign_sinks.get(thread_id)
        if entry is None or entry[1] != self._client_generation() or thread_id not in self._foreign_direct:
            # Not (yet) routing directly on THIS connection: queue it. The
            # (re-)subscribe handover passes it on in order, so a new wire's
            # events never mix into, or overtake, an older one's.
            return False
        turn_id = _codex_message_turn_id(message)
        verdict = self._foreign_verdict(thread_id, turn_id)
        if verdict == "defer":
            self._parked_turns.setdefault(thread_id, set()).add(turn_id)
            return False
        if verdict == "ours":
            return False
        entry[0](thread_id, message)
        return True

    def _hand_off_foreign(self, thread_id: str, message: dict[str, Any], turn_id: str) -> bool:
        """Give a foreign-turn message to the mirror; False = no mirror."""
        entry = self._foreign_sinks.get(thread_id)
        if entry is None:
            return False
        try:
            entry[0](thread_id, message)
        except Exception as exc:  # noqa: BLE001 - mirroring must never break a drain
            _log_degrade("codex_foreign_handoff_failed", thread_id=thread_id, error=exc)
        return True

    def _hand_over_queued_foreign(self, thread_id: str) -> None:
        """Pass the idle queue's foreign-turn messages to the mirror, in order; then route direct.

        Runs when nothing consumes the thread's queue any more: after a
        subscribe, after our drain ends, after a submit resolves. Synchronous on
        purpose: with no await between taking the queue and switching direct
        routing on (and unparking), the reader cannot route a later message
        of a turn ahead of its earlier, still-queued ones. While a drain is
        listening the queue is the drain's; it hands foreign turns over itself
        and calls this again when it ends. Messages still undecidable (a
        turn/start of ours in flight) stay queued and parked until the submit
        resolves, which calls this again.
        """
        if not self.foreign_sink_current(thread_id) or self._draining.get(thread_id):
            return
        take_queued = getattr(self.client, "take_queued", None)
        if take_queued is None:
            return
        parked = self._parked_turns.setdefault(thread_id, set())
        pending = thread_id in self._pending_starts

        def foreign(message: dict[str, Any]) -> bool:
            turn_id = _codex_message_turn_id(message)
            if not turn_id or self.started_turn(turn_id):
                return False
            if pending:
                parked.add(turn_id)
                return False
            return True

        taken = take_queued(thread_id, foreign)
        if taken is None:
            return
        for message in taken:
            self._hand_off_foreign(thread_id, message, _codex_message_turn_id(message))
        if not pending:
            parked.clear()
        self._foreign_direct.add(thread_id)

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            structured_input=True,
            structured_output=True,
            permission_callback=True,
            ask_user_question=True,
            set_model=False,
            resume_after_complete=True,
            external_tui_takeover=True,
        )

    def _with_sandbox_override(self, params: dict[str, Any]) -> dict[str, Any]:
        # Omit the key entirely rather than sending a placeholder: any value we
        # send wins over the profile's sandbox_mode, so "no opinion" has to be
        # expressed as absence. thread/start and thread/resume both accept the
        # key and both must carry the override — sending it only on start meant
        # an explicit `read-only` silently lapsed the moment the app-server
        # restarted and the thread was cold-resumed from disk.
        if self.sandbox_override is not None:
            params["sandbox"] = self.sandbox_override
        return params

    def _apply_effective_sandbox(self, result: Any, thread_id: str, *, method: str) -> None:
        """Record (and vet) the sandbox the app-server says it actually applied.

        thread/start and thread/resume both return the effective SandboxPolicy.
        Without reading it back, a permission drift — wrong CODEX_HOME, a
        profile edited underneath us, an override the server declined — is
        completely invisible: the bot just quietly runs with more or less
        privilege than intended.
        """
        policy = (result or {}).get("sandbox") if isinstance(result, dict) else None
        effective = ""
        if isinstance(policy, dict):
            effective = str(policy.get("type") or "")
        if not effective:
            return
        if thread_id:
            self.effective_sandbox[thread_id] = effective

        if effective == "dangerFullAccess" and not self.allowlist_configured and not self.unrestricted_without_allowlist_ok:
            _log_degrade(
                "codex_unsandboxed_without_allowlist",
                method=method,
                thread_id=thread_id,
                effective=effective,
            )
            raise UnsafeSandboxError(UNSAFE_SANDBOX_MESSAGE)

        if self.sandbox_override is not None:
            expected = _CODEX_SANDBOX_POLICY_TYPES.get(self.sandbox_override)
            if expected and effective != expected:
                _log_degrade(
                    "codex_sandbox_override_ignored",
                    method=method,
                    thread_id=thread_id,
                    requested=self.sandbox_override,
                    effective=effective,
                )

    async def launch(self, spec: LaunchSpec) -> TransportHandle:
        params = self._with_sandbox_override(
            {
                "cwd": spec.cwd,
                "approvalPolicy": self.approval_policy,
                "ephemeral": self.ephemeral,
            }
        )
        result = await self.client.request("thread/start", params)
        thread_id = _codex_thread_id(result)
        self._released_threads.discard(thread_id)
        self._apply_effective_sandbox(result, thread_id, method="thread/start")
        if self.environment_context and thread_id:
            self._env_context_pending.add(thread_id)
        return TransportHandle(
            handle_id=f"codex-{uuid.uuid4().hex}",
            transport_kind=self.kind,
            ref={"thread_id": thread_id, "cwd": spec.cwd},
        )

    async def resume_thread(self, thread_id: str, *, cwd: str) -> TransportHandle:
        if not thread_id:
            raise ValueError("thread_id is required for Codex resume")
        params = self._with_sandbox_override({"threadId": thread_id, "cwd": cwd})
        result = await self.client.request("thread/resume", params)
        resumed = _codex_thread_id(result) or thread_id
        # A resumed thread is live again; a stale release mark would make the
        # next drain quit on its first batch boundary.
        self._released_threads.discard(resumed)
        self._apply_effective_sandbox(result, resumed, method="thread/resume")
        if self.environment_context and resumed not in self._env_context_delivered:
            self._env_context_pending.add(resumed)
        return TransportHandle(
            handle_id=f"codex-{uuid.uuid4().hex}",
            transport_kind=self.kind,
            ref={"thread_id": resumed, "cwd": cwd},
        )

    async def resume(self, spec: ResumeSpec) -> TransportHandle:
        thread_id = str(spec.resume_ref.get("thread_id", ""))
        return await self.resume_thread(thread_id, cwd=spec.cwd)

    async def submit_turn(
        self,
        handle: TransportHandle,
        turn: TurnInput,
        idempotency_key: str,
    ) -> None:
        thread_id = handle.ref["thread_id"]
        # Attachment paths must ride the prompt (no attachment channel here),
        # and the result must never be blank — see EMPTY_TURN_PLACEHOLDER:
        # a blank turn/start input poisons the thread's history for good.
        text = _compose_turn_text(turn)
        if thread_id in self._env_context_pending:
            text = f"{self.environment_context}\n\n{text}"
        if not text.strip():
            text = EMPTY_TURN_PLACEHOLDER
        self._pending_starts.add(thread_id)
        try:
            result = await self.client.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [
                        {"type": "text", "text": text, "text_elements": []},
                    ],
                    "approvalPolicy": self.approval_policy,
                    "idempotencyKey": idempotency_key,
                },
            )
        except BaseException:
            # No drain will run for a failed submit: hand over what the
            # in-flight window parked and route the rest of those turns direct.
            self._pending_starts.discard(thread_id)
            self._hand_over_queued_foreign(thread_id)
            raise
        self._pending_starts.discard(thread_id)
        turn = result.get("turn") if isinstance(result, dict) else None
        turn_id = str(turn.get("id", "") or "") if isinstance(turn, dict) else ""
        if thread_id and turn_id:
            self._active_turns[thread_id] = turn_id
        if turn_id:
            self._started_turns[turn_id] = None
            if len(self._started_turns) > CODEX_STARTED_TURNS_LIMIT:
                del self._started_turns[next(iter(self._started_turns))]
        # Our turn id is known now: what the in-flight window parked can be
        # told apart — hand the foreign part over (a later drain gets ours).
        self._hand_over_queued_foreign(thread_id)
        # Only a confirmed turn/start clears the mark: a failed submit keeps
        # the context pending so the retry carries it again.
        self._env_context_pending.discard(thread_id)
        self._env_context_delivered.add(thread_id)

    async def events(self, handle: TransportHandle):
        """Drain our turn (see ``_listen_own_turn``), then release parked foreign turns.

        While this drain ran, another client's turn that began during our
        turn/start stayed on the queue path so its messages kept their order.
        Once nobody consumes the queue, hand what is left to the mirror and
        let the rest of those turns route directly.
        """
        thread_id = str(handle.ref.get("thread_id", "") or "")
        self._draining[thread_id] = self._draining.get(thread_id, 0) + 1
        try:
            async for event in self._listen_own_turn(handle):
                yield event
        finally:
            remaining = self._draining.get(thread_id, 1) - 1
            if remaining > 0:
                self._draining[thread_id] = remaining
            else:
                self._draining.pop(thread_id, None)
            self._hand_over_queued_foreign(thread_id)

    async def _listen_own_turn(self, handle: TransportHandle):
        """Listen until the turn really ends — not until one batch returns.

        ``client.events()`` is a BOUNDED collector: it returns after
        ``event_timeout`` seconds whether or not the turn finished. Treating
        one batch as the whole stream is what made a long turn look like a
        broken one — ``_drain_events`` saw the iteration end mid-turn and
        flipped the session to ERROR_RECOVERABLE, which has no self-healing
        path, so everything the agent produced afterwards was dropped and only
        the user's next message could resume it (2026-07-30: a 68-minute turn
        completed normally at 09:50:03, its final answer never reached the
        channel because the drain had given up at 09:49:24).

        So the batch boundary is an implementation detail here, not a stream
        end: keep re-entering the collector while the turn stays open. Only a
        genuinely silent worker ends the listen, and it says so out loud —
        with a synthetic TURN_COMPLETED, exactly like the Claude ceiling path
        (see ``_bridged_event_stream``), so the drain reads a closed turn
        instead of a mid-turn failure.
        """
        thread_id = handle.ref["thread_id"]
        silent_since = time.monotonic()
        parked_on_human = False
        while True:
            batch_started = time.monotonic()
            raw_events = await self.client.events(thread_id)
            # Under the shared app-server daemon another client (the TUI) can
            # run turns on this same thread; their events queue up here too.
            # Only our own turn is this drain's output, and only its
            # turn/completed ends it — otherwise a TUI turn's reply is posted
            # as ours and our real answer waits in the queue for the next
            # message. Unknown own turn (e.g. listening without a submit):
            # keep the old, unfiltered behavior.
            own_turn = self._active_turns.get(thread_id, "")
            turn_closed = False
            # Liveness counts only what this drain consumes: a stalled turn of
            # ours must still hit the silence ceiling while the TUI keeps busy.
            own_traffic = False
            delta_parts: list[str] = []
            delta_model = ""
            for raw_event in raw_events:
                event_turn = _codex_message_turn_id(raw_event)
                if own_turn and event_turn and event_turn != own_turn:
                    if self.started_turn(event_turn):
                        continue  # a late event of an earlier turn of ours
                    handed_off = self._hand_off_foreign(thread_id, raw_event, event_turn)
                    if not handed_off and raw_event.get("method") == "turn/completed":
                        _log_degrade("codex_foreign_turn_skipped", thread_id=thread_id, turn_id=event_turn)
                    continue
                # With our turn known, only events of that turn prove it alive:
                # thread-level ones (thread/status/changed, hook/*) also fire
                # for the TUI's turns and would keep a stalled turn of ours
                # from ever hitting the ceiling. They are still consumed.
                if not own_turn or event_turn == own_turn:
                    own_traffic = True
                event = self._convert_event(raw_event, thread_id=thread_id)
                if event is None:
                    continue
                if event.type == AgentEventType.TURN_DELTA:
                    delta_parts.append(str(event.payload.get("text", "")))
                    if not delta_model:
                        delta_model = str(event.payload.get("model", "") or "")
                    continue
                if delta_parts:
                    delta_payload: dict[str, Any] = {"text": "".join(delta_parts)}
                    if delta_model:
                        delta_payload["model"] = delta_model
                    yield AgentEvent(AgentEventType.TURN_DELTA, delta_payload)
                    delta_parts = []
                    delta_model = ""
                yield event
                # A HITL prompt parks the turn on a human. Keep listening: the
                # answer goes back over the same wire (answer_request), and the
                # agent's continuation arrives on this very stream. Returning
                # here would end the only consumer — the decision would be
                # written, the agent would resume, and everything it produced
                # afterwards would sit in the queue until an unrelated user
                # message happened to start a new drain. That is the same
                # silent loss this whole change exists to remove.
                if event.type in self._TURN_PARKING_EVENTS:
                    parked_on_human = True
                else:
                    # The agent produced something, so it is running again —
                    # the card was answered (or withdrawn). Clearing this
                    # matters for how a LATER silence is reported: a stalled
                    # agent must not be described as "waiting for you".
                    parked_on_human = False
                if event.type == AgentEventType.TURN_COMPLETED:
                    turn_closed = True
            if turn_closed and thread_id:
                # The turn is over: this thread's model is now baked into the
                # session record (via the TURN_COMPLETED payload above). Drop
                # the cache entry so a long-lived runtime cannot accumulate
                # one ~100B mapping per finished thread forever.
                self._thread_models.pop(thread_id, None)
                # Only if it is still the turn that just closed: the consumer
                # may have submitted the next turn while the completion was
                # yielded, and that turn's id must survive for its drain.
                if self._active_turns.get(thread_id) == own_turn:
                    self._active_turns.pop(thread_id, None)
            if delta_parts:
                delta_payload: dict[str, Any] = {"text": "".join(delta_parts)}
                if delta_model:
                    delta_payload["model"] = delta_model
                yield AgentEvent(AgentEventType.TURN_DELTA, delta_payload)
            if turn_closed:
                return
            if thread_id in self._released_threads:
                # close_session shut this thread down while the batch was in
                # flight. Ending with a synthetic completion (not a bare
                # return) keeps the drain from reading a closed session's
                # stream end as a mid-turn failure.
                self._released_threads.discard(thread_id)
                yield AgentEvent(AgentEventType.TURN_COMPLETED, {"message": ""})
                return
            if own_traffic:
                # Traffic for our turn proves the worker is alive and working.
                silent_since = time.monotonic()
                continue
            if time.monotonic() - silent_since < self.event_silence_ceiling:
                # A real collector already blocked for its own timeout before
                # returning empty, so this loop is normally self-throttling.
                # Do not rely on that: a client that returns empty instantly
                # (a closed transport, a stub) would spin a hot loop for the
                # whole ceiling window.
                idle = time.monotonic() - batch_started
                if idle < self._EMPTY_BATCH_MIN_INTERVAL:
                    await asyncio.sleep(self._EMPTY_BATCH_MIN_INTERVAL - idle)
                continue
            _log_degrade(
                "codex_event_silence_ceiling",
                thread_id=thread_id,
                ceiling_seconds=self.event_silence_ceiling,
                parked_on_human=parked_on_human,
            )
            if parked_on_human:
                # Waiting on a person, not on a stalled agent. Say so, and do
                # NOT synthesize a completion: the turn is genuinely unfinished
                # and the card may still be answered. ERROR_RECOVERABLE is the
                # honest state here — the next message resumes it.
                yield AgentEvent(
                    AgentEventType.TURN_DELTA,
                    {
                        "text": (
                            f"⚠️ 这个回合在等你回应，已经等了 "
                            f"{_humanize_seconds(self.event_silence_ceiling)}，先停止监听。"
                            "直接回复可重新拉起会话。"
                        )
                    },
                )
                return
            yield AgentEvent(
                AgentEventType.TURN_DELTA,
                {
                    "text": (
                        f"⚠️ 会话已静默 {_humanize_seconds(self.event_silence_ceiling)}"
                        "——没有任何新输出，回合也没有结束，已停止本次会话监听。"
                        "直接回复可重新拉起会话。"
                    )
                },
            )
            # Close the synthetic warning turn so the drain does not read the
            # ended stream as a mid-turn failure and flip the session to
            # ERROR_RECOVERABLE.
            #
            # Known limit: this transport exposes no interrupt, so the server
            # side of the turn is not actually cancelled — after a full hour of
            # total silence it is almost certainly dead, but a late event from
            # it would arrive on a stream nobody is draining.
            yield AgentEvent(AgentEventType.TURN_COMPLETED, {"message": ""})
            return

    async def approve_permission(self, handle: TransportHandle | None, rid: str, decision: dict[str, Any]) -> None:
        responder = getattr(self.client, "answer_request", None)
        if responder is None:
            raise CapabilityUnsupported("Codex app-server request responses are not available")
        await responder(rid, self._approval_response_for_request(rid, decision))

    async def answer_user_question(
        self,
        handle: TransportHandle | None,
        rid: str,
        answers: dict[str, Any],
    ) -> None:
        responder = getattr(self.client, "answer_request", None)
        if responder is None:
            raise CapabilityUnsupported("Codex app-server request responses are not available")
        await responder(rid, self._question_response_for_request(rid, answers))

    async def shutdown(self, handle: TransportHandle, mode: str) -> ControlResult:
        """Stop this thread's work and stop listening to it.

        There is no per-session process to reap: ONE app-server serves every
        thread under this CODEX_HOME, so killing it would take every other
        session down with it. "Exit" is therefore two THREAD-scoped calls, and
        nothing process- or server-scoped:

        - ``turn/interrupt`` so the agent actually stops working. Unsubscribing
          alone would only stop us WATCHING a turn that keeps running, with
          whatever sandbox it holds.
        - ``thread/unsubscribe`` so the server stops pushing this thread's
          notifications at us. Verified against codex 0.144.5: it drops THIS
          connection's subscription to THIS thread — a second call answers
          ``notSubscribed``, sibling threads stay loaded, subscribed and
          readable, and the server keeps serving. The thread itself stays in
          the daemon's loaded set (there is no non-destructive unload;
          thread/archive and thread/delete are not that), and the rollout stays
          on disk, so ``thread/resume`` brings it back.

        Best-effort by design: the ledger close must not hinge on the server
        answering, or a wedged daemon would pin the session at "running" with
        no way for the user to end it.
        """
        thread_id = str((handle.ref or {}).get("thread_id", "") or "")
        turn_id = self._active_turns.pop(thread_id, "")
        self._env_context_pending.discard(thread_id)
        self._env_context_delivered.discard(thread_id)
        self._thread_models.pop(thread_id, None)
        self.effective_sandbox.pop(thread_id, None)
        self.unsubscribe_foreign_mirror(thread_id)
        if not thread_id:
            return ControlResult(True, state="stopped")
        # Marked before the calls, not after: a drain sitting in
        # client.events() must find the mark whenever its batch returns, even
        # if the requests below hang or fail.
        self._released_threads.add(thread_id)
        if turn_id:
            try:
                await self.client.request(
                    "turn/interrupt",
                    {"threadId": thread_id, "turnId": turn_id},
                )
            except Exception as exc:
                _log_degrade(
                    "codex_turn_interrupt_failed",
                    thread_id=thread_id,
                    turn_id=turn_id,
                    mode=mode,
                    error=exc,
                )
        try:
            await self.client.request("thread/unsubscribe", {"threadId": thread_id})
        except Exception as exc:
            _log_degrade(
                "codex_thread_unsubscribe_failed",
                thread_id=thread_id,
                mode=mode,
                error=exc,
            )
        return ControlResult(True, state="stopped")

    async def restart_backend(self) -> None:
        """Replace the app-server so it re-reads config.toml (/reload).

        Profile-wide by construction: one app-server serves every thread under
        this CODEX_HOME, and the config snapshot it is holding — ``mcp_servers``
        above all — only refreshes when the process is replaced. Sibling
        sessions therefore lose their connection too and reconnect on their
        next message; the caller is expected to say so out loud.

        Per-thread caches are dropped with the process they described. Keeping
        them would let a stale model or sandbox reading survive onto the new
        server, which is the exact class of drift ``_apply_effective_sandbox``
        exists to catch.
        """
        restart = getattr(self.client, "restart", None)
        if restart is None:
            raise CapabilityUnsupported(
                "this Codex app-server client cannot be restarted"
            )
        await _maybe_await(restart())
        self._thread_models.clear()
        self._foreign_sinks.clear()
        self._foreign_direct.clear()
        self._pending_starts.clear()
        self._parked_turns.clear()
        self._active_turns.clear()
        self.effective_sandbox.clear()
        # NOT cleared, deliberately:
        #
        # _released_threads — it is a signal to drains, not a cache of the old
        #   process. A drain that returns from a batch right after the restart
        #   and finds no mark falls through to client.events(), whose
        #   _ensure_started SPAWNS A NEW app-server and then listens on a
        #   thread nobody resumed there: empty batches until the silence
        #   ceiling (an hour). Keeping the mark lets it close out cleanly at
        #   the next batch boundary. resume_thread discards it when the thread
        #   genuinely comes back.
        # _env_context_delivered — the preamble is in the thread's HISTORY,
        #   not in the server; a restart does not un-send it, and re-marking
        #   would re-inject it on every resume after a reload.

    async def set_model(self, handle: TransportHandle, model: str) -> ControlResult:
        raise CapabilityUnsupported("Codex app-server model switching is not verified")

    def _convert_event(self, event: dict[str, Any], *, thread_id: str = "") -> AgentEvent | None:
        event_type = str(event.get("type", "") or event.get("method", ""))
        if self._is_hitl_server_request(event):
            rid = str(event.get("id", "") or "")
            if rid:
                self._pending_server_requests[rid] = dict(event)
            payload = event.get("params", {})
            if not isinstance(payload, dict):
                payload = {}
            if event_type in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
                "item/permissions/requestApproval",
            }:
                return self._permission_event_from_server_request(rid, event_type, payload)
            if event_type in {
                "item/tool/requestUserInput",
                "mcpServer/elicitation/request",
            }:
                return self._ask_user_event_from_server_request(rid, event_type, payload)
            return None
        if event_type == "event_msg":
            event_payload = event.get("payload", {})
            if not isinstance(event_payload, dict):
                return None
            codex_event_type = str(event_payload.get("type", "") or "")
            if codex_event_type == "thread_settings_applied":
                settings = event_payload.get("thread_settings", {})
                if isinstance(settings, dict):
                    model = str(settings.get("model", "") or "")
                    if model and thread_id:
                        self._thread_models[thread_id] = model
            elif codex_event_type == "turn_context":
                model = str(event_payload.get("model", "") or "")
                if model and thread_id:
                    self._thread_models[thread_id] = model
            model = self._thread_models.get(thread_id, "") if thread_id else ""
            if codex_event_type == "agent_message":
                message = str(event_payload.get("message", "") or "")
                if not message:
                    return None
                payload_out: dict[str, Any] = {"text": message}
                if model:
                    payload_out["model"] = model
                return AgentEvent(AgentEventType.TURN_DELTA, payload_out)
            if codex_event_type == "task_complete":
                payload_out = {
                    "message": str(event_payload.get("last_agent_message", "") or ""),
                    "usage": event_payload.get("usage", {}),
                    "status": "completed",
                    "thread_id": str(event_payload.get("threadId", "") or event_payload.get("thread_id", "") or ""),
                    "turn_id": str(event_payload.get("turn_id", "") or event_payload.get("turnId", "") or ""),
                }
                if model:
                    payload_out["model"] = model
                return AgentEvent(AgentEventType.TURN_COMPLETED, payload_out)
            tool_event = _codex_tool_event(codex_event_type, event_payload)
            if tool_event is not None:
                return tool_event
            self._log_unhandled_event_type(f"event_msg/{codex_event_type}")
            return None
        payload = event.get("params", event)
        if not isinstance(payload, dict):
            payload = {"value": payload}
        # The app-server ALSO emits bare turn_context records (they live in
        # the rollout; some wire paths surface them as top-level objects).
        # Absorb the model the same way as the event_msg variant so the cache
        # is populated no matter which shape arrives first.
        if event_type == "turn_context":
            context_payload = event.get("payload", {})
            if not isinstance(context_payload, dict):
                context_payload = payload
            model = str(context_payload.get("model", "") or "")
            if model and thread_id:
                self._thread_models[thread_id] = model
        if event_type == "thread/settings/updated":
            settings = payload.get("threadSettings", {})
            if isinstance(settings, dict):
                model = str(settings.get("model", "") or "")
                if model and thread_id:
                    self._thread_models[thread_id] = model
        model = self._thread_models.get(thread_id, "") if thread_id else ""
        if event_type == "item/agentMessage/delta":
            payload_out: dict[str, Any] = {"text": str(payload.get("delta", ""))}
            if model:
                payload_out["model"] = model
            return AgentEvent(AgentEventType.TURN_DELTA, payload_out)
        tool_event = _codex_tool_event(event_type, payload)
        if tool_event is not None:
            return tool_event
        item_type = _codex_item_type(payload)
        if event_type == "turn/completed":
            payload_out = {
                "message": str(payload.get("message", "")),
                "usage": payload.get("usage", {}),
                "status": payload.get("status", "completed"),
            }
            if model:
                payload_out["model"] = model
            return AgentEvent(AgentEventType.TURN_COMPLETED, payload_out)
        if event_type == "error":
            return self._turn_error_event(payload)
        self._log_unhandled_event_type(event_type, item_type=item_type)
        return None

    def _log_unhandled_event_type(self, event_type: str, *, item_type: str = "") -> None:
        """Say — once per type — that codex told us something we ignore.

        Everything unrecognised used to be dropped without a trace, so a whole
        class of "codex said it and nobody listened" was invisible: the
        2026-08-07 outage sat behind exactly that blind spot. Once per type per
        process keeps a chatty stream (token_count, sub_agent_activity) from
        flooding the log while still surfacing the vocabulary we don't cover.

        The key carries the item type because `item/started` and
        `item/completed` are envelopes, not types: one `agentMessage` claims
        the key and every later item subtype — including a tool type codex
        adds in the next release — goes dark. Deduping per subtype keeps the
        stream just as quiet while making the coverage gap visible, which is
        the whole point of the log (ADR 0062/0063).
        """
        if not event_type:
            return
        key = f"{event_type}/{item_type}" if item_type else event_type
        if key in self._logged_unhandled_types:
            return
        self._logged_unhandled_types.add(key)
        if item_type:
            _log_degrade(
                "codex_event_type_unhandled", event_type=event_type, item_type=item_type
            )
        else:
            _log_degrade("codex_event_type_unhandled", event_type=event_type)

    @staticmethod
    def _turn_error_event(payload: dict[str, Any]) -> AgentEvent:
        """codex app-server `error` notification → a channel-visible event.

        Shape (app-server v2 `ErrorNotification`):
            {error: {message, additionalDetails?, codexErrorInfo?},
             threadId, turnId, willRetry}

        `willRetry` is the whole point: while it is true codex is still
        backing off and retrying, so the note rides the tool-progress card
        rather than the thread. When it flips to false the turn is lost, and
        that is a bubble — otherwise the turn just ends in silence, which is
        the 2026-08-07 outage verbatim (six upstream 400s, no user-visible
        trace of any of them).
        """
        error = payload.get("error")
        if not isinstance(error, dict):
            error = {}
        kind, status = _codex_error_kind(error.get("codexErrorInfo"))
        # Kind and HTTP status lead, free text follows and is what gets cut.
        # The other order loses the status entirely: the progress card caps
        # its lines, so a thousand-character upstream message would push
        # "HTTP 429" — the single most diagnostic token — off the end.
        head = f"{kind}（HTTP {status}）" if kind and status else (kind or (f"HTTP {status}" if status else ""))
        free_text = " · ".join(
            dict.fromkeys(
                part
                for part in (
                    _compact_tool_summary(error.get("message"), limit=200),
                    _compact_tool_summary(error.get("additionalDetails"), limit=120),
                )
                if part
            )
        )
        summary = " · ".join(part for part in (head, free_text) if part) or "上游返回了一个未描述的错误"
        if bool(payload.get("willRetry")):
            return AgentEvent(
                AgentEventType.TURN_NARRATION,
                {
                    "text": f"⚠️ 上游报错，正在重试：{summary}",
                    # Not real output: a turn whose only trace is retry notes
                    # must still get the end-of-turn "no output" warning.
                    "diagnostic": True,
                },
            )
        return AgentEvent(
            AgentEventType.SESSION_ERROR,
            {
                "reason": "codex_turn_error",
                "message": f"⚠️ 本轮失败（代理已放弃重试）：{summary}",
                "turn_id": str(payload.get("turnId", "") or ""),
            },
        )

    @classmethod
    def _is_hitl_server_request(cls, event: dict[str, Any]) -> bool:
        return (
            "id" in event
            and "method" in event
            and "result" not in event
            and "error" not in event
            and str(event.get("method", "")) in cls._HITL_SERVER_REQUEST_METHODS
        )

    @staticmethod
    def _permission_event_from_server_request(
        rid: str,
        method: str,
        payload: dict[str, Any],
    ) -> AgentEvent:
        if method == "item/commandExecution/requestApproval":
            tool_input = {
                "native_method": method,
                "thread_id": str(payload.get("threadId", "") or ""),
                "turn_id": str(payload.get("turnId", "") or ""),
                "item_id": str(payload.get("itemId", "") or ""),
                "command": str(payload.get("command", "") or ""),
                "cwd": str(payload.get("cwd", "") or ""),
                "reason": str(payload.get("reason", "") or ""),
            }
            return AgentEvent(
                AgentEventType.PERMISSION_REQUESTED,
                {
                    "rid": rid,
                    "tool_name": "Command",
                    "tool_input": tool_input,
                    "actions": _codex_approval_actions(
                        payload.get("availableDecisions"),
                        default=["accept", "acceptForSession", "decline", "cancel"],
                    ),
                    "high_risk": True,
                },
            )
        if method == "item/fileChange/requestApproval":
            tool_input = {
                "native_method": method,
                "thread_id": str(payload.get("threadId", "") or ""),
                "turn_id": str(payload.get("turnId", "") or ""),
                "item_id": str(payload.get("itemId", "") or ""),
                "grant_root": str(payload.get("grantRoot", "") or ""),
                "reason": str(payload.get("reason", "") or ""),
            }
            return AgentEvent(
                AgentEventType.PERMISSION_REQUESTED,
                {
                    "rid": rid,
                    "tool_name": "File change",
                    "tool_input": tool_input,
                    "actions": ["accept", "acceptForSession", "decline", "cancel"],
                    "high_risk": True,
                },
            )
        permissions = payload.get("permissions", {})
        if not isinstance(permissions, dict):
            permissions = {}
        tool_input = {
            "native_method": method,
            "thread_id": str(payload.get("threadId", "") or ""),
            "turn_id": str(payload.get("turnId", "") or ""),
            "item_id": str(payload.get("itemId", "") or ""),
            "cwd": str(payload.get("cwd", "") or ""),
            "reason": str(payload.get("reason", "") or ""),
            "permissions": permissions,
        }
        return AgentEvent(
            AgentEventType.PERMISSION_REQUESTED,
            {
                "rid": rid,
                "tool_name": "Permission profile",
                "tool_input": tool_input,
                "actions": ["accept", "acceptForSession", "decline"],
                "high_risk": True,
            },
        )

    @staticmethod
    def _ask_user_event_from_server_request(
        rid: str,
        method: str,
        payload: dict[str, Any],
    ) -> AgentEvent:
        if method == "item/tool/requestUserInput":
            raw_questions = payload.get("questions", [])
            questions: list[dict[str, Any]] = []
            if isinstance(raw_questions, list):
                for question in raw_questions:
                    if not isinstance(question, dict):
                        continue
                    options: list[str] = []
                    raw_options = question.get("options", [])
                    if isinstance(raw_options, list):
                        for option in raw_options:
                            if isinstance(option, dict):
                                options.append(str(option.get("label", "") or ""))
                            else:
                                options.append(str(option))
                    questions.append(
                        {
                            "id": str(question.get("id", "") or ""),
                            "header": str(question.get("header", "") or ""),
                            "prompt": str(question.get("question", "") or question.get("header", "") or ""),
                            "options": [option for option in options if option],
                            "allow_other": bool(question.get("isOther", False)),
                            "is_secret": bool(question.get("isSecret", False)),
                        }
                    )
            if not questions:
                questions = [{"prompt": "Input required", "options": [], "id": ""}]
            return AgentEvent(
                AgentEventType.ASK_USER_REQUESTED,
                {
                    "rid": rid,
                    "native_method": method,
                    "questions": questions,
                },
            )
        return AgentEvent(
            AgentEventType.ASK_USER_REQUESTED,
            {
                "rid": rid,
                "native_method": method,
                "questions": _codex_mcp_elicitation_questions(payload),
            },
        )

    def _approval_response_for_request(self, rid: str, decision: dict[str, Any]) -> dict[str, Any]:
        request = self._pending_server_requests.get(rid, {})
        method = str(request.get("method", "") or "")
        params = request.get("params", {})
        if not isinstance(params, dict):
            params = {}
        tool_input = decision.get("_tool_input", {})
        if not isinstance(tool_input, dict):
            tool_input = {}
        if not method:
            method = str(tool_input.get("native_method", "") or "")
        action = _codex_normalize_approval_action(str(decision.get("action", "") or ""))
        if method == "item/permissions/requestApproval":
            requested = params.get("permissions", {})
            if not isinstance(requested, dict) or not requested:
                requested = tool_input.get("permissions", {})
            if not isinstance(requested, dict):
                requested = {}
            if action in {"accept", "acceptForSession"}:
                permissions = {
                    key: value
                    for key, value in {
                        "network": requested.get("network"),
                        "fileSystem": requested.get("fileSystem"),
                    }.items()
                    if value is not None
                }
                return {
                    "permissions": permissions,
                    "scope": "session" if action == "acceptForSession" else "turn",
                }
            return {"permissions": {}, "scope": "turn", "strictAutoReview": True}
        if method == "item/fileChange/requestApproval":
            return {"decision": action if action in {"accept", "acceptForSession", "decline", "cancel"} else "decline"}
        return {
            "decision": _codex_native_decision_for_action(
                action,
                params.get("availableDecisions"),
            )
        }

    def _question_response_for_request(self, rid: str, answers: dict[str, Any]) -> dict[str, Any]:
        request = self._pending_server_requests.get(rid, {})
        method = str(request.get("method", "") or "")
        params = request.get("params", {})
        if not isinstance(params, dict):
            params = {}
        questions_from_answers = answers.get("_questions", [])
        if not method and isinstance(questions_from_answers, list):
            method = "item/tool/requestUserInput"
        if method == "mcpServer/elicitation/request":
            questions = answers.get("_questions", [])
            if not isinstance(questions, list) or not questions:
                questions = _codex_mcp_elicitation_questions(params)
            action = "accept"
            content: dict[str, Any] = {}
            for index, question in enumerate(questions):
                if not isinstance(question, dict):
                    continue
                question_id = str(question.get("id", "") or index)
                value = answers.get(index, answers.get(str(index)))
                if question_id == "mcp_elicitation_action":
                    action = str(_codex_first_answer_value({0: value}) or "accept")
                    continue
                content[question_id] = _codex_mcp_answer_value(value, question)
            if action not in {"accept", "decline", "cancel"}:
                content = {"answer": action}
                action = "accept"
            return {
                "action": action,
                "content": None if action != "accept" else content,
                "_meta": params.get("_meta"),
            }
        raw_questions = params.get("questions", [])
        if not isinstance(raw_questions, list) or not raw_questions:
            raw_questions = questions_from_answers
        native_answers: dict[str, dict[str, list[str]]] = {}
        if isinstance(raw_questions, list):
            for index, question in enumerate(raw_questions):
                if not isinstance(question, dict):
                    continue
                question_id = str(question.get("id", "") or index)
                value = answers.get(index, answers.get(str(index)))
                values = value if isinstance(value, list) else [value]
                native_answers[question_id] = {
                    "answers": [str(item) for item in values if item is not None]
                }
        return {"answers": native_answers}


# codex app-server v2 `CodexErrorInfo` → 中文短标签。Unit variants are bare
# strings; the ones that carry an upstream HTTP status are single-key objects
# (`{"responseStreamDisconnected": {"httpStatusCode": 400}}`).
_CODEX_ERROR_LABELS = {
    "contextWindowExceeded": "上下文超限",
    "sessionBudgetExceeded": "会话预算耗尽",
    "usageLimitExceeded": "用量超限",
    "serverOverloaded": "上游过载",
    "cyberPolicy": "安全策略拦截",
    "internalServerError": "上游内部错误",
    "unauthorized": "鉴权失败",
    "badRequest": "请求被拒（400）",
    "threadRollbackFailed": "会话回滚失败",
    "sandboxError": "沙箱错误",
    "other": "",
    "httpConnectionFailed": "连接失败",
    "responseStreamConnectionFailed": "响应流连接失败",
    "responseStreamDisconnected": "响应流中断",
    "responseTooManyFailedAttempts": "重试次数耗尽",
    "activeTurnNotSteerable": "当前回合不可插话",
}


def _codex_error_kind(info: Any) -> tuple[str, str]:
    """(label, http_status) from a `codexErrorInfo`; ("", "") when absent."""
    if isinstance(info, str):
        return _CODEX_ERROR_LABELS.get(info, info), ""
    if isinstance(info, dict) and info:
        key = next(iter(info))
        detail = info.get(key)
        status = ""
        if isinstance(detail, dict) and detail.get("httpStatusCode") is not None:
            status = str(detail["httpStatusCode"])
        return _CODEX_ERROR_LABELS.get(key, key), status
    return "", ""


def _codex_tool_event(event_type: str, payload: dict[str, Any]) -> AgentEvent | None:
    normalized = event_type.lower()
    normalized_compact = re.sub(r"[^a-z0-9]+", "", normalized)
    item = payload.get("item")
    item_type = _codex_item_type(payload)
    item_type_compact = re.sub(r"[^a-z0-9]+", "", item_type.lower())
    event_is_tool_like = _codex_tool_like_name(normalized_compact)
    spec = _CODEX_TOOL_ITEM_SPECS.get(item_type_compact)
    if not event_is_tool_like and spec is None:
        return None
    if isinstance(item, dict):
        payload = {**payload, **item}
        normalized = f"{normalized}/{item_type.lower()}"
        normalized_compact = re.sub(r"[^a-z0-9]+", "", normalized)
    # One summary for the whole card, computed once. It has to outlive the
    # branch split: the progress card upserts by tool_id, so a completion that
    # falls back to "Tool completed" *overwrites* what the start event showed.
    # A web search that ends therefore used to lose its query.
    item_summary: Any = spec.summary(payload) if spec is not None else None
    tool_name = str(
        payload.get("toolName")
        or payload.get("tool_name")
        or payload.get("name")
        # mcpToolCall / dynamicToolCall / collabAgentToolCall spell it `tool`.
        or payload.get("tool")
        or payload.get("commandName")
        or payload.get("command_name")
        or (spec.name if spec is not None else "")
        or ("command" if payload.get("command") else "")
        or "tool"
    )
    tool_id = str(
        payload.get("toolCallId")
        or payload.get("tool_call_id")
        or payload.get("itemId")
        or payload.get("id")
        or ""
    )
    # The item's own status outranks the method name. codex reports a *declined*
    # patch as `item/completed` with `status: "declined"`; going by the method
    # name alone turned a rejected edit into a success card.
    status = str(payload.get("status", "") or "").lower()
    state = _CODEX_TOOL_STATUS_STATES.get(status, "")
    if not state:
        state = _codex_tool_state_from_event_name(normalized_compact)
    if payload.get("error") is not None:
        state = "failed"
    if state == "failed":
        return AgentEvent(
            AgentEventType.TOOL_FAILED,
            {
                "tool_id": tool_id,
                "tool_name": tool_name,
                "summary": _compact_tool_summary(
                    payload.get("error")
                    or payload.get("reason")
                    # Keep the paths/query on the failure card too — "which
                    # patch was rejected" is the first thing anyone asks.
                    or item_summary
                    or "Tool failed"
                ),
            },
        )
    if state == "completed":
        return AgentEvent(
            AgentEventType.TOOL_COMPLETED,
            {
                "tool_id": tool_id,
                "tool_name": tool_name,
                # The type's own extractor wins: a generic `summary` field
                # would otherwise be free to carry a whole patch onto the card.
                "summary": _compact_tool_summary(
                    item_summary or payload.get("summary") or "Tool completed"
                ),
            },
        )
    if state == "started":
        return AgentEvent(
            AgentEventType.TOOL_STARTED,
            {
                "tool_id": tool_id,
                "tool_name": tool_name,
                "summary": _compact_tool_summary(
                    item_summary
                    or payload.get("arguments")
                    or payload.get("args")
                    or payload.get("input")
                    or payload.get("command")
                    or payload.get("summary")
                    or ""
                ),
            },
        )
    return None


def _codex_approval_actions(value: Any, *, default: list[str]) -> list[str]:
    if not isinstance(value, list):
        return list(default)
    actions: list[str] = []
    for item in value:
        if isinstance(item, str):
            actions.append(item)
        elif isinstance(item, dict) and len(item) == 1:
            actions.append(str(next(iter(item.keys()))))
    return actions or list(default)


def _codex_normalize_approval_action(action: str) -> str:
    mapping = {
        "allow": "accept",
        "allow_once": "accept",
        "always_allow": "acceptForSession",
        "deny": "decline",
        "reject": "decline",
    }
    return mapping.get(action, action)


def _codex_native_decision_for_action(action: str, available: Any) -> Any:
    if isinstance(available, list):
        for item in available:
            if isinstance(item, str) and item == action:
                return item
            if isinstance(item, dict) and action in item:
                return item
    if action in {"accept", "acceptForSession", "decline", "cancel"}:
        return action
    return "decline"


def _codex_first_answer_value(answers: dict[str, Any]) -> Any:
    if not answers:
        return ""
    for key in (0, "0"):
        if key in answers:
            value = answers[key]
            if isinstance(value, list):
                return value[0] if value else ""
            return value
    first = next(iter(answers.values()))
    if isinstance(first, list):
        return first[0] if first else ""
    return first


def _codex_mcp_elicitation_questions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    message = str(payload.get("message", "") or "MCP input required")
    mode = str(payload.get("mode", "") or "")
    schema = payload.get("requestedSchema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if mode in {"form", "openai/form"} and isinstance(properties, dict) and properties:
        required = schema.get("required", []) if isinstance(schema, dict) else []
        required_ids = {str(item) for item in required} if isinstance(required, list) else set()
        questions: list[dict[str, Any]] = []
        for field_id, definition in properties.items():
            if not isinstance(definition, dict):
                continue
            options, allow_multiple = _codex_mcp_schema_options(definition)
            title = str(definition.get("title", "") or field_id)
            description = str(definition.get("description", "") or "").strip()
            prompt = title if not description else f"{title}\n{description}"
            questions.append(
                {
                    "id": str(field_id),
                    "prompt": prompt,
                    "options": options,
                    "allow_other": not options,
                    "allow_multiple": allow_multiple,
                    "is_secret": bool(definition.get("format") == "password"),
                    "required": str(field_id) in required_ids,
                    "value_type": _codex_mcp_schema_value_type(definition),
                }
            )
        if questions:
            return questions
    return [
        {
            "id": "mcp_elicitation_action",
            "prompt": message,
            "options": ["accept", "decline", "cancel"],
            "allow_other": mode in {"form", "openai/form"},
        }
    ]


def _codex_mcp_schema_options(definition: dict[str, Any]) -> tuple[list[str], bool]:
    schema = definition
    allow_multiple = False
    if str(schema.get("type", "")) == "array" and isinstance(schema.get("items"), dict):
        allow_multiple = True
        schema = schema["items"]
    enum_values = schema.get("enum")
    if isinstance(enum_values, list):
        return [str(item) for item in enum_values], allow_multiple
    any_of = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(any_of, list):
        const_values = [
            item.get("const")
            for item in any_of
            if isinstance(item, dict) and item.get("const") is not None
        ]
        if const_values:
            return [str(item) for item in const_values], allow_multiple
    if str(schema.get("type", "")) == "boolean":
        return ["true", "false"], False
    return [], allow_multiple


def _codex_mcp_schema_value_type(definition: dict[str, Any]) -> str:
    value_type = definition.get("type")
    if isinstance(value_type, str):
        if value_type == "array" and isinstance(definition.get("items"), dict):
            item_type = definition["items"].get("type")
            return f"array:{item_type}" if isinstance(item_type, str) else "array"
        return value_type
    return ""


def _codex_mcp_answer_value(value: Any, question: dict[str, Any]) -> Any:
    value_type = str(question.get("value_type", "") or "")
    allow_multiple = bool(question.get("allow_multiple", False))
    if isinstance(value, list):
        raw_value: Any = value if allow_multiple else (value[0] if value else "")
    else:
        raw_value = value
    if allow_multiple:
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        return [_codex_mcp_scalar_answer(item, value_type.removeprefix("array:")) for item in values]
    return _codex_mcp_scalar_answer(raw_value, value_type)


def _codex_mcp_scalar_answer(value: Any, value_type: str) -> Any:
    if value is None:
        return ""
    if value_type == "boolean":
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
    if value_type == "integer":
        try:
            return int(str(value).strip())
        except ValueError:
            return value
    if value_type == "number":
        try:
            return float(str(value).strip())
        except ValueError:
            return value
    return value


def _codex_tool_like_name(value: str) -> bool:
    if not value:
        return False
    if value in {"usermessage", "agentmessage", "reasoning"}:
        return False
    return any(token in value for token in ("tool", "function", "command", "exec", "shell", "bash"))


class _CodexToolItemSpec(NamedTuple):
    """How one codex `ThreadItem` variant renders as a tool card.

    `name` is the fallback card title — the payload's own `toolName`/`tool`
    wins when it has one. `summary` extracts what the card body should say,
    and is used by the started/completed/failed branches alike so a card never
    loses its subject halfway through.
    """

    name: str
    summary: Callable[[dict[str, Any]], Any]


def _codex_file_change_summary(changes: Any, *, limit: int = _TOOL_SUMMARY_LIMIT) -> str:
    """Paths that fit, then a count of the ones that don't.

    A `fileChange` item carries the full diff of every change; the patch body
    must stay off the card for the same reason command output does.

    The count has to be computed *inside* the same budget `_compact_tool_summary`
    enforces, not appended and hoped for: long paths would otherwise push the
    "+N more" tail past the limit and get chopped off, leaving a card that both
    cuts a path mid-word and hides how big the edit was — the exact thing the
    tail exists to prevent.
    """
    if not isinstance(changes, list):
        return ""
    paths = [
        str(change["path"])
        for change in changes
        if isinstance(change, dict) and change.get("path")
    ]
    if not paths:
        return ""
    total = len(paths)
    for shown in range(min(_CODEX_FILE_CHANGE_PATHS_SHOWN, total), 0, -1):
        rendered = ", ".join(paths[:shown])
        if shown < total:
            rendered += f" (+{total - shown} more, {total} files)"
        if len(rendered) <= limit:
            return rendered
    # Even one path overflows: keep the count, drop the path list.
    return f"{total} files" if total > 1 else paths[0][:limit]


# How many paths a fileChange card lists before switching to a count; fewer are
# shown when they do not fit the summary budget.
_CODEX_FILE_CHANGE_PATHS_SHOWN = 5

# Every `ThreadItem` variant that is agent tool activity, taken from the
# app-server schema (`codex app-server generate-json-schema` → ThreadItem) —
# NOT inferred from the type's spelling. The substring probe above still runs
# for legacy `event_msg` event names, but item types are matched exactly here.
#
# Exactness is the point: a bare "search" or "file" token would swallow
# `fuzzyFileSearch/sessionCompleted`, the file picker's autocomplete push,
# and turn every keystroke into a card.
#
# Deliberately absent (schema variants that are not tool activity, or have no
# user-facing form yet): userMessage, hookPrompt, agentMessage, reasoning and
# plan (rendered by their own paths), imageGeneration, imageView, sleep,
# subAgentActivity, enteredReviewMode, exitedReviewMode, contextCompaction.
# `test_codex_tool_item_specs_cover_every_schema_variant` fails when codex adds
# a variant that appears in neither list.
_CODEX_TOOL_ITEM_SPECS: dict[str, _CodexToolItemSpec] = {
    "commandexecution": _CodexToolItemSpec("command", lambda p: p.get("command")),
    "mcptoolcall": _CodexToolItemSpec("mcp_tool", lambda p: p.get("arguments")),
    "dynamictoolcall": _CodexToolItemSpec("tool", lambda p: p.get("arguments")),
    "collabagenttoolcall": _CodexToolItemSpec("collab_agent", lambda p: p.get("prompt")),
    "websearch": _CodexToolItemSpec("web_search", lambda p: p.get("query")),
    "filechange": _CodexToolItemSpec(
        "apply_patch", lambda p: _codex_file_change_summary(p.get("changes"))
    ),
    # codex 0.153.4. Unreachable today: this item is the echo of tool output
    # the *client* submits in `turn/start.toolOutput`, and walkcode never sets
    # that field (guarded by test_no_code_path_submits_tool_output). Carded
    # anyway because the alternative is the 2026-08-07 failure mode — an
    # unclassified tool item drops into the unhandled-event log and the user
    # sees nothing. No status field on this one, so the card state falls back
    # to the method name via `_codex_tool_state_from_event_name`; the payload's
    # own `name` (the function) outranks the label below.
    "functioncalloutput": _CodexToolItemSpec("tool_output", lambda p: p.get("output")),
}

# `PatchApplyStatus` / `McpToolCallStatus` values → card state. Checked before
# the method name, because `item/completed` is the envelope for a declined
# patch too.
_CODEX_TOOL_STATUS_STATES = {
    "failed": "failed",
    "error": "failed",
    "errored": "failed",
    "declined": "failed",
    "completed": "completed",
    "succeeded": "completed",
    "success": "completed",
    "done": "completed",
    "running": "started",
    "inprogress": "started",
    "in_progress": "started",
}


def _codex_tool_state_from_event_name(normalized_compact: str) -> str:
    """Fall back to the method name when the item carries no status."""
    for state, tokens in (
        ("failed", ("failed", "error", "errored")),
        ("completed", ("completed", "succeeded", "success", "done", "result", "end")),
        ("started", ("started", "start", "call", "created", "begin", "running")),
    ):
        if any(token in normalized_compact for token in tokens):
            return state
    return ""


def _codex_item_type(payload: dict[str, Any]) -> str:
    """The `item.type` of an `item/*` notification, or "" when there is none."""
    item = payload.get("item")
    if not isinstance(item, dict):
        return ""
    return str(item.get("type", "") or "")


def _codex_thread_id(result: dict[str, Any]) -> str:
    thread_id = str(result.get("threadId", "") or "")
    if thread_id:
        return thread_id
    thread = result.get("thread", {})
    if isinstance(thread, dict):
        return str(thread.get("id", "") or "")
    return ""
