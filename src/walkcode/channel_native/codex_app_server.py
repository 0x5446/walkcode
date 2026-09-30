"""Codex app-server JSON-RPC clients: the stdio child process and the managed daemon (control socket)."""

from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import hashlib
import json
import os
import secrets
import struct
import time

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .models import _log_degrade, TransportUnavailable


class _StreamFailure:
    """Queued marker that the wire died at this point in the stream.

    Sent through the per-thread queue rather than raised out of band so a
    failure cannot overtake the events that arrived before it.

    ``generation`` pins it to the connection that died. A marker left over
    from an earlier wire — the turn it belonged to ended before the queue was
    drained past it — must not surface on a later turn running over a healthy
    reconnected wire.
    """

    __slots__ = ("error", "generation")

    def __init__(self, error: BaseException, generation: int):
        self.error = error
        self.generation = generation


def _as_transport_unavailable(error: BaseException) -> TransportUnavailable:
    if isinstance(error, TransportUnavailable):
        return error
    return TransportUnavailable(str(error) or "Codex app-server stream failed")


class CodexStdioAppServerClient:
    # thread/resume returns the thread metadata plus initialTurnsPage as a
    # single JSON line; real sessions easily exceed asyncio's default 64 KiB
    # StreamReader limit (observed 733 KiB for a 55 MB rollout). readline()
    # then clears its buffer and raises ValueError, which used to surface as
    # an opaque takeover "resume_failed". Raise the high-water mark instead —
    # chunked readuntil accumulation is NOT cancellation-safe (a wait_for
    # timeout mid-line would drop drained bytes and desync the stream).
    _STDOUT_LIMIT = 64 * 1024 * 1024
    _BUFFERED_NOTIFICATIONS_MAX = 256
    # Shutdown budget for the app-server subprocess. See _discard_process for
    # why SIGTERM comes first and why both waits are bounded.
    _TERMINATE_GRACE_SECONDS = 5.0
    _KILL_GRACE_SECONDS = 2.0

    def __init__(
        self,
        *,
        command: tuple[str, ...] = ("codex", "app-server", "--stdio"),
        request_timeout: float = 30.0,
        event_timeout: float = 180.0,
        event_idle_timeout: float = 2.0,
        codex_home: str = "",
    ):
        self.command = command
        self.request_timeout = request_timeout
        self.event_timeout = event_timeout
        self.event_idle_timeout = event_idle_timeout
        self.codex_home = codex_home
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        # Guards startup and the WRITE side only. Reading is owned by the
        # resident reader task below — the two must not share a lock: a read
        # that holds it for the whole event window (the old shape) blocks
        # every turn/start and approval answer behind it, which is why a
        # message sent from the channel could sit unseen for minutes.
        self._lock = asyncio.Lock()
        # Bounded: account/rateLimits/updated and friends carry no threadId
        # and arrive all day while nobody listens; an unbounded list grew to
        # thousands of stale globals, all dumped on the next drain.
        self._buffered_notifications: collections.deque[dict[str, Any]] = collections.deque(
            maxlen=self._BUFFERED_NOTIFICATIONS_MAX
        )
        self._thread_less_methods_logged: set[str] = set()
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
        self._stderr_task: asyncio.Task | None = None
        self._reader_task: asyncio.Task | None = None
        self._pending_responses: dict[int, asyncio.Future] = {}
        self._thread_queues: dict[str, asyncio.Queue] = {}
        # Who is listening RIGHT NOW, by thread. Deliberately separate from
        # _thread_queues: a queue outlives its listener (it holds events that
        # arrive between two drains), so "a queue exists" says nothing about
        # whether that thread is still live. Routing decisions must use this.
        self._active_listeners: dict[str, int] = {}
        # Per-thread stream failure awaiting delivery, as (generation, error).
        # Survives a reconnect for the consumer that has not seen it yet, but
        # is pinned to the connection that broke so it cannot leak onto a
        # later turn running over a healthy wire.
        self._thread_failures: dict[str, tuple[int, BaseException]] = {}
        # Events pulled off a queue by a call that then died (cancelled at a
        # handoff, raised mid-batch). queue.get() is destructive, so they are
        # parked here and re-served, in order, ahead of the queue.
        self._thread_pushback: dict[str, list[dict[str, Any]]] = {}
        # Bumped on every reader start. Everything the previous wire left
        # behind (queued failure markers, carried errors) is stale once this
        # moves.
        self._connection_generation = 0
        # Set when the reader dies; replayed to every later caller so a dead
        # transport fails loudly instead of hanging on an empty queue.
        self._stream_error: BaseException | None = None
        # ADR 0065: set by the transport. Called from _dispatch for every
        # thread-addressed message; returns True when it took the message
        # (another client's turn on a thread we share) so it must not enter
        # the thread queue. None = every message is ours, as before.
        self.foreign_router: Callable[[str, dict[str, Any]], bool] | None = None

    @property
    def connection_generation(self) -> int:
        return self._connection_generation

    def take_queued(
        self, thread_id: str, predicate: Callable[[dict[str, Any]], bool]
    ) -> list[dict[str, Any]] | None:
        """Remove and return buffered messages of an idle thread matching ``predicate``.

        Covers the pushback of an aborted ``events()`` call and the queue, in
        that (arrival) order; order is preserved for taken and kept messages. Only for a
        thread nobody is listening to: a live ``events()`` call owns its queue
        (a second consumer would split the stream), so this returns None then.
        """
        if self._active_listeners.get(thread_id, 0) > 0:
            return None
        taken: list[dict[str, Any]] = []
        # What an aborted events() call had already taken is older than the
        # queue: look at it first and keep the rest of it in place.
        pushback = self._thread_pushback.pop(thread_id, [])
        kept_back = [m for m in pushback if not predicate(m)]
        taken.extend(m for m in pushback if predicate(m))
        if kept_back:
            self._thread_pushback[thread_id] = kept_back
        queue = self._thread_queues.get(thread_id)
        if queue is None:
            return taken
        kept: list[Any] = []
        while not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, dict) and predicate(item):
                taken.append(item)
            else:
                kept.append(item)
        for item in kept:
            queue.put_nowait(item)
        return taken

    def _route_foreign(self, thread_id: str, message: dict[str, Any]) -> bool:
        router = self.foreign_router
        if router is None:
            return False
        try:
            return bool(router(thread_id, message))
        except Exception as exc:  # noqa: BLE001 - the reader must survive any router bug
            # The reader is the only consumer of the wire; an exception here
            # would fail the whole stream. Fall back to the thread queue (the
            # pre-ADR-0065 path) and say so.
            _log_degrade("codex_foreign_router_failed", thread_id=thread_id, error=exc)
            return False

    def _subprocess_env(self) -> dict[str, str] | None:
        # CODEX_HOME pins the profile's auth/config/daemon state; None keeps
        # plain environment inheritance for the no-profile setup.
        if not self.codex_home:
            return None
        return {**os.environ, "CODEX_HOME": self.codex_home}

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        # Only the send is serialized; the answer arrives through the reader
        # task, so a long event window can no longer delay a submit.
        async with self._lock:
            await self._ensure_started()
            request_id = self._next_id
            self._next_id += 1
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending_responses[request_id] = future
            try:
                await self._send({"id": request_id, "method": method, "params": params})
            except BaseException:
                self._pending_responses.pop(request_id, None)
                raise
        try:
            response = await asyncio.wait_for(future, timeout=self.request_timeout)
        except asyncio.TimeoutError as exc:
            raise TransportUnavailable("Codex app-server request timed out") from exc
        finally:
            self._pending_responses.pop(request_id, None)
        result = response.get("result", {})
        return result if isinstance(result, dict) else {"value": result}

    async def events(self, thread_id: str) -> list[dict[str, Any]]:
        """Collect this thread's events for up to ``event_timeout`` seconds.

        Still bounded — the caller re-enters while the turn stays open — but
        it no longer owns the wire. The resident reader task does, and it runs
        whether or not anyone is inside this call, so events produced between
        two drains land in the queue instead of going unread.
        """
        # A failure the previous call could not deliver (it had events in hand,
        # and delivering them came first) is raised BEFORE _ensure_started().
        # Order matters: a dead reader makes _ensure_started() reconnect, which
        # bumps the generation — checking afterwards would find the carried
        # error "stale", drop it, and hand the caller an empty batch. The open
        # turn would then look merely quiet and get a synthetic completion,
        # which is the very swallowing this carry-over exists to prevent.
        carried = self._thread_failures.pop(thread_id, None)
        if carried is not None and carried[0] == self._connection_generation:
            raise _as_transport_unavailable(carried[1])
        async with self._lock:
            await self._ensure_started()
        queue = self._queue_for(thread_id)
        self._active_listeners[thread_id] = self._active_listeners.get(thread_id, 0) + 1
        delivered = False
        collected: list[dict[str, Any]] = []
        try:
            collected = self._take_buffered(thread_id)
            if _contains_codex_turn_completed(collected, thread_id) or _contains_codex_hitl_server_request(
                collected,
                thread_id,
            ):
                delivered = True
                return collected
            deadline = time.monotonic() + self.event_timeout
            while time.monotonic() < deadline:
                if not collected and queue.empty():
                    # Nothing in hand and nothing queued: a dead wire can be
                    # reported straight away. With anything queued we must go
                    # through the queue instead — the failure sentinel sits
                    # behind the real events, and short-circuiting here would
                    # skip them, which is the loss this design prevents.
                    self._raise_stream_error_if_dead()
                timeout = min(self.event_idle_timeout, max(0.0, deadline - time.monotonic()))
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=timeout)
                except (asyncio.TimeoutError, TimeoutError):
                    continue
                if isinstance(message, _StreamFailure):
                    if message.generation != self._connection_generation:
                        # Left over from a wire that has since been replaced:
                        # the turn it belonged to already ended (its terminal
                        # event returned ahead of this marker). Raising it now
                        # would fail a healthy turn on the new connection.
                        continue
                    # The failure was queued BEHIND the events that arrived
                    # before it, so everything real has already been collected.
                    # Deliver those first and carry the error to the next call
                    # — dropping them to raise immediately would lose exactly
                    # the turn output this whole change exists to protect.
                    if collected:
                        self._thread_failures[thread_id] = (message.generation, message.error)
                        delivered = True
                        return collected
                    raise _as_transport_unavailable(message.error)
                collected.append(message)
                if _is_codex_hitl_server_request_message(message):
                    delivered = True
                    return collected
                if _notification_method(message) == "turn/completed":
                    delivered = True
                    return collected
            delivered = True
            return collected
        finally:
            if not delivered and collected:
                # Cancelled or raised while holding events taken off the queue.
                # queue.get() is destructive, so without this they would be
                # gone for good — a drain cancelled by a handoff would silently
                # eat whatever it had already pulled. Stash them ahead of the
                # queue for the next call on this thread.
                self._thread_pushback.setdefault(thread_id, []).extend(collected)
            remaining = self._active_listeners.get(thread_id, 1) - 1
            if remaining > 0:
                self._active_listeners[thread_id] = remaining
            else:
                self._active_listeners.pop(thread_id, None)
                # Reclaim the queue only when it holds nothing: a non-empty
                # queue is undelivered output for this thread, and the next
                # drain must still find it.
                if queue.empty() and self._thread_queues.get(thread_id) is queue:
                    self._thread_queues.pop(thread_id, None)

    def _queue_for(self, thread_id: str) -> asyncio.Queue:
        queue = self._thread_queues.get(thread_id)
        if queue is None:
            queue = asyncio.Queue()
            self._thread_queues[thread_id] = queue
        return queue

    def _live_listener_threads(self) -> list[str]:
        return [thread_id for thread_id, count in self._active_listeners.items() if count > 0]

    def _raise_stream_error_if_dead(self) -> None:
        # Fast path for a wire that died before anything reached this thread.
        # Callers must check that both their batch AND the queue are empty
        # first — undelivered events always outrank the failure.
        error = self._stream_error
        if error is None:
            return
        raise _as_transport_unavailable(error)

    async def _reader_loop(self) -> None:
        """Own the read side for the transport's whole lifetime.

        One reader, always running: responses go to their waiting future,
        everything else is routed to a thread queue. Nothing depends on a
        drain being in progress at the moment a message arrives.
        """
        try:
            while True:
                try:
                    message = await self._read_message(timeout=None)
                except (asyncio.TimeoutError, TimeoutError):
                    # An unbounded read should never time out; if a transport
                    # reports one anyway, that is "nothing yet", not a death.
                    continue
                self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - replayed to every caller
            self._fail_stream(exc)

    def _dispatch(self, message: dict[str, Any]) -> None:
        if _is_response_message(message) or _is_error_message(message):
            future = self._pending_responses.pop(message.get("id"), None)
            if future is not None and not future.done():
                if _is_error_message(message):
                    error = message.get("error", {})
                    detail = (
                        str(error.get("message", "Codex app-server request failed"))
                        if isinstance(error, dict)
                        else "Codex app-server request failed"
                    )
                    future.set_exception(TransportUnavailable(detail))
                else:
                    future.set_result(message)
            return
        if not _is_codex_hitl_server_request_message(message) and not _is_notification_message(message):
            return
        thread_id = _notification_thread_id(message)
        if thread_id:
            if self._route_foreign(thread_id, message):
                return
            self._queue_for(thread_id).put_nowait(message)
            return
        # No threadId on the message. With exactly one thread listening right
        # now it can only be that one; otherwise guessing would cross-talk
        # sessions (they share one app-server process), so park it in the
        # shared buffer for whichever drain can unambiguously claim it later.
        #
        # The liveness test is _active_listeners, NOT _thread_queues: queues
        # are never torn down while they hold undelivered events, so "a queue
        # exists" would mean "some thread once ran here" and, after two
        # threads had ever run, every unaddressed message would be buffered
        # forever with no claimant — a silent loss of exactly the final replies
        # this change exists to protect.
        live = self._live_listener_threads()
        if len(live) == 1:
            self._queue_for(live[0]).put_nowait(message)
            return
        method = _notification_method(message)
        if method not in self._thread_less_methods_logged:
            self._thread_less_methods_logged.add(method)
            _log_degrade("codex_event_without_thread_id", method=method, live_listeners=len(live))
        self._buffered_notifications.append(message)

    def _fail_stream(self, exc: BaseException) -> None:
        self._stream_error = exc
        _log_degrade(
            "codex_reader_stream_failed",
            error=f"{type(exc).__name__}: {exc}",
            pending_requests=len(self._pending_responses),
            live_listeners=len(self._live_listener_threads()),
            queued_threads=len(self._thread_queues),
            buffered=len(self._buffered_notifications),
        )
        # Queue the failure behind whatever each thread has already received,
        # so a consumer drains its real events first and only then learns the
        # wire is gone. Raising out of band instead would let the error jump
        # ahead of undelivered output.
        failure = _StreamFailure(exc, self._connection_generation)
        for queue in self._thread_queues.values():
            queue.put_nowait(failure)
        for future in list(self._pending_responses.values()):
            if not future.done():
                future.set_exception(_as_transport_unavailable(exc))
        self._pending_responses.clear()

    async def _stop_reader(self) -> None:
        task, self._reader_task = self._reader_task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def restart(self) -> None:
        """Replace the app-server process so it re-reads config.toml.

        This exists for /reload and nothing else. The app-server snapshots
        ``mcp_servers`` when the PROCESS starts; ``thread/resume`` reuses that
        snapshot and only ``thread/start`` re-reads the file, so an MCP added
        after the process came up is unreachable for every existing thread
        until the process is replaced.

        Only tears down — ``_ensure_started`` spawns the replacement on the
        next request, which keeps the "one place builds the wire" invariant.
        In-flight requests are failed rather than left to time out: their
        answers died with the process.
        """
        async with self._lock:
            await self._stop_reader()
            self._fail_stream(TransportUnavailable("codex app-server restarted for /reload"))
            await self._discard_process()

    async def answer_request(self, request_id: str, result: dict[str, Any]) -> None:
        async with self._lock:
            await self._ensure_started()
            await self._send({"id": request_id, "result": result})

    async def _ensure_started(self) -> None:
        if self._process is not None and self._process.returncode is None and self._reader_alive():
            return
        # A live process whose reader died is unusable: nothing would ever
        # route its responses. Tear it down and rebuild both together.
        await self._stop_reader()
        if self._process is not None:
            await self._discard_process()
        self._process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._subprocess_env(),
            limit=self._STDOUT_LIMIT,
        )
        # app-server logs to stderr for its whole life. asyncio buffers an
        # unread pipe in memory up to 2x `limit` (128 MiB here) before it
        # stops reading, so nobody reading it is a slow leak that ends in a
        # blocked server. Drain it continuously and keep only the tail, which
        # is what explains an exit.
        self._stderr_tail = collections.deque(maxlen=20)
        self._stderr_task = asyncio.create_task(self._drain_stderr(self._process, self._stderr_tail))
        await self._handshake()

    async def _handshake(self) -> None:
        """Initialize, then hand the wire to the reader task.

        The handshake response is read inline on purpose: the reader must not
        be running yet, or it would consume the reply before this call sees it.
        """
        await self._send(
            {
                "id": 0,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "walkcode", "version": "channel-native-v3"},
                    "capabilities": {},
                },
            }
        )
        await self._read_response(0, timeout=self.request_timeout)
        self._start_reader()

    def _reader_alive(self) -> bool:
        return self._reader_task is not None and not self._reader_task.done()

    def _start_reader(self) -> None:
        # A fresh wire clears the connection-level failure so new callers do
        # not inherit the dead transport's error. _thread_failures is NOT
        # cleared: those belong to consumers that have not seen them yet, and
        # a reconnect does not un-break the stream they were reading.
        self._stream_error = None
        self._connection_generation += 1
        self._reader_task = asyncio.ensure_future(self._reader_loop())

    async def _discard_process(self) -> None:
        # Startup/shutdown callers hold self._lock; the reader task also lands
        # here on an unrecoverable desync, without it (it is about to fail the
        # stream anyway, and a racing _send just gets TransportUnavailable).
        # Clearing _process makes the next request start a fresh subprocess.
        # Buffered notifications are KEPT: they are complete, validly-parsed
        # events (agent text, turn/completed) that already happened — killing
        # the transport does not un-happen them, and dropping them would
        # silently lose session events.
        process, self._process = self._process, None
        stderr_task, self._stderr_task = self._stderr_task, None
        try:
            await self._terminate_process(process)
        finally:
            # A child that inherited stderr can hold the pipe open after the
            # wrapper exits. Cancelling the drain alone leaves asyncio reading
            # that pipe into a buffer nobody consumes, so close the process's
            # pipes too; every restart would otherwise leak one of each.
            if stderr_task is not None:
                stderr_task.cancel()
            transport = getattr(process, "_transport", None)
            if transport is not None:
                with contextlib.suppress(Exception):
                    transport.close()

    async def _terminate_process(self, process: asyncio.subprocess.Process | None) -> None:
        if process is None or process.returncode is not None:
            return
        # SIGTERM, not SIGKILL. `codex app-server --stdio` is a thin node
        # wrapper around the vendor binary: SIGTERM lets the wrapper shut the
        # real server down, SIGKILL kills only the wrapper and leaves the
        # server orphaned — still holding our stdout pipe, so `await
        # process.wait()` never returns even though returncode is already set.
        # Measured on 0.144.5: terminate returns in 0.01s with no survivors;
        # kill hangs the caller AND leaks the app-server, which for /reload
        # would mean the stale config snapshot outlives the "restart".
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=self._TERMINATE_GRACE_SECONDS)
            return
        except (asyncio.TimeoutError, TimeoutError):
            pass
        except Exception:
            return
        _log_degrade(
            "codex_app_server_terminate_escalated",
            pid=process.pid,
            grace_seconds=self._TERMINATE_GRACE_SECONDS,
        )
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        # Bounded on purpose: a SIGKILLed wrapper can leave the pipe held open
        # by its child, and blocking here forever would wedge every caller
        # behind self._lock.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=self._KILL_GRACE_SECONDS)

    async def _send(self, message: dict[str, Any]) -> None:
        process = self._require_process()
        assert process.stdin is not None
        process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await process.stdin.drain()

    async def _read_response(self, request_id: int, *, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = await self._read_message(timeout=max(0.0, deadline - time.monotonic()))
            if _is_response_message(message) and message.get("id") == request_id:
                return message
            if _is_error_message(message) and message.get("id") == request_id:
                error = message.get("error", {})
                if isinstance(error, dict):
                    raise TransportUnavailable(str(error.get("message", "Codex app-server request failed")))
                raise TransportUnavailable("Codex app-server request failed")
            if _is_notification_message(message) or _is_codex_hitl_server_request_message(message):
                self._buffered_notifications.append(message)
        raise TransportUnavailable("Codex app-server request timed out")

    async def _read_message(self, *, timeout: float | None) -> dict[str, Any]:
        # timeout=None waits indefinitely — the resident reader has no deadline
        # of its own; a turn may legitimately think for an hour.
        process = self._require_process()
        assert process.stdout is not None
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError from exc
        except ValueError as exc:
            # readline() clears its buffer and raises ValueError when a line
            # exceeds the stream limit; the stream is desynced beyond repair
            # (the over-long line's tail is still in the pipe), so discard the
            # process — _ensure_started would otherwise reuse it and parse
            # that tail as the next response.
            await self._discard_process()
            raise TransportUnavailable(
                f"Codex app-server response line exceeded {self._STDOUT_LIMIT} bytes"
            ) from exc
        if not line:
            if self._stderr_task is not None:
                # Let the drain pick up the last lines the exiting server wrote.
                with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                    await asyncio.wait_for(asyncio.shield(self._stderr_task), timeout=0.1)
            stderr = "\n".join(self._stderr_tail).strip()
            reason = stderr or f"Codex app-server exited with code {process.returncode}"
            raise TransportUnavailable(reason)
        try:
            message = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise TransportUnavailable("Codex app-server returned invalid JSON") from exc
        if not isinstance(message, dict):
            raise TransportUnavailable("Codex app-server returned non-object JSON")
        return message

    _STDERR_LINE_MAX = 2000

    @classmethod
    async def _drain_stderr(cls, process: asyncio.subprocess.Process, tail: collections.deque) -> None:
        # read(), not readline(): readline() raises on a line longer than the
        # stream limit, which would end the drain while the server lives on.
        if process.stderr is None:
            return
        pending = b""
        with contextlib.suppress(Exception):
            while chunk := await process.stderr.read(64 * 1024):
                *lines, pending = (pending + chunk).split(b"\n")
                pending = pending[-cls._STDERR_LINE_MAX :]
                for line in lines:
                    tail.append(line[: cls._STDERR_LINE_MAX].decode("utf-8", errors="replace").rstrip())
        if pending:
            tail.append(pending.decode("utf-8", errors="replace").rstrip())

    def _take_buffered(self, thread_id: str) -> list[dict[str, Any]]:
        # Anything a previous, aborted call had already taken comes first —
        # it is strictly older than what is still queued.
        taken = self._thread_pushback.pop(thread_id, [])
        # Thread-less messages may only be claimed when nobody else is
        # listening. Several TUI sessions share one app-server process, so the
        # permissive "no threadId means mine" rule would hand one session's
        # events to whichever drain happened to run first. Keying this on
        # CURRENT listeners (not on queues, which outlive their listeners)
        # keeps a message claimable once the other sessions have gone quiet,
        # instead of stranding it in the buffer forever.
        claim_unaddressed = not [
            other
            for other, count in self._active_listeners.items()
            if count > 0 and other != thread_id
        ]
        kept: list[dict[str, Any]] = []
        for message in self._buffered_notifications:
            message_thread_id = _notification_thread_id(message)
            if message_thread_id == thread_id or (not message_thread_id and claim_unaddressed):
                taken.append(message)
            else:
                kept.append(message)
        self._buffered_notifications.clear()
        self._buffered_notifications.extend(kept)
        return taken

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise TransportUnavailable("Codex app-server is not started")
        return self._process


class CodexManagedAppServerClient(CodexStdioAppServerClient):
    _WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(
        self,
        *,
        socket_path: str = "",
        daemon_command: tuple[str, ...] = ("codex", "app-server", "daemon", "start"),
        request_timeout: float = 30.0,
        event_timeout: float = 180.0,
        event_idle_timeout: float = 2.0,
        codex_home: str = "",
    ):
        super().__init__(
            command=("codex", "app-server", "daemon"),
            request_timeout=request_timeout,
            event_timeout=event_timeout,
            event_idle_timeout=event_idle_timeout,
            codex_home=codex_home,
        )
        self.socket_path = socket_path or str(
            _codex_home_path(codex_home) / "app-server-control" / "app-server-control.sock"
        )
        self.daemon_command = daemon_command
        self._daemon_checked = False
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def _ensure_started(self) -> None:
        if not self._daemon_checked:
            await self._start_daemon()
            self._daemon_checked = True
        if self._writer is not None and not self._writer.is_closing() and self._reader_alive():
            return
        # Same rule as the stdio client: a connection whose reader died routes
        # nothing, so rebuild the pair rather than reusing the socket.
        await self._stop_reader()
        await self._connect_websocket()
        await self._send(
            {
                "id": 0,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "walkcode", "version": "channel-native-v3"},
                    "capabilities": {},
                },
            }
        )
        await self._read_response(0, timeout=self.request_timeout)
        await self._send({"method": "initialized", "params": {}})
        self._start_reader()

    async def restart(self) -> None:
        """Restart the managed daemon, not just our socket to it.

        Dropping the connection would accomplish nothing here: the config
        snapshot that /reload is after lives in the DAEMON process, which
        outlives every client. ``codex app-server daemon restart`` replaces it;
        the torn-down socket then reconnects on the next request.
        """
        async with self._lock:
            await self._stop_reader()
            self._fail_stream(TransportUnavailable("codex app-server daemon restarted for /reload"))
            self._close_websocket()
            await self._run_daemon_command("restart")
            # Force the next _ensure_started back through _start_daemon: the
            # restart may still be settling, and the cached "checked" flag
            # would skip the one call that waits for it.
            self._daemon_checked = False

    async def _start_daemon(self) -> None:
        await self._run_daemon_command("start")

    def _daemon_command_for(self, action: str) -> tuple[str, ...]:
        # daemon_command carries its own verb ("... daemon start"); swap the
        # verb rather than appending, or restart becomes "daemon start restart".
        base = tuple(self.daemon_command)
        if base and base[-1] in {"start", "restart", "stop"}:
            base = base[:-1]
        return (*base, action)

    async def _run_daemon_command(self, action: str) -> None:
        process = await asyncio.create_subprocess_exec(
            *self._daemon_command_for(action),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._subprocess_env(),
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
            raise TransportUnavailable(
                detail or f"failed to {action} Codex app-server daemon"
            )

    async def _connect_websocket(self) -> None:
        try:
            reader, writer = await asyncio.open_unix_connection(self.socket_path)
        except OSError as exc:
            raise TransportUnavailable(f"Codex app-server socket unavailable: {self.socket_path}") from exc
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        writer.write(request.encode("ascii"))
        await writer.drain()
        try:
            response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=self.request_timeout)
        except Exception as exc:
            writer.close()
            await _wait_closed_safely(writer)
            raise TransportUnavailable("Codex app-server websocket handshake timed out") from exc
        header = response.decode("iso-8859-1", errors="replace")
        if not header.startswith("HTTP/1.1 101") and not header.startswith("HTTP/1.0 101"):
            writer.close()
            await _wait_closed_safely(writer)
            raise TransportUnavailable(f"Codex app-server websocket handshake failed: {header.splitlines()[0] if header else 'empty response'}")
        expected_accept = base64.b64encode(
            hashlib.sha1((key + self._WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if expected_accept not in header:
            writer.close()
            await _wait_closed_safely(writer)
            raise TransportUnavailable("Codex app-server websocket handshake returned invalid accept key")
        self._reader = reader
        self._writer = writer

    async def _send(self, message: dict[str, Any]) -> None:
        writer = self._require_writer()
        writer.write(_websocket_text_frame(json.dumps(message)))
        await writer.drain()

    async def _read_message(self, *, timeout: float | None) -> dict[str, Any]:
        # timeout=None waits indefinitely, for the resident reader task; a
        # deadline of +inf keeps the ping/pong and non-text-frame skips on the
        # single loop below instead of forking a second code path.
        reader = self._require_reader()
        deadline = float("inf") if timeout is None else time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = None if deadline == float("inf") else max(0.0, deadline - time.monotonic())
            try:
                opcode, payload = await asyncio.wait_for(_read_websocket_frame(reader), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise TimeoutError from exc
            except Exception as exc:
                raise TransportUnavailable("Codex app-server websocket read failed") from exc
            if opcode == 0x8:
                self._close_websocket()
                raise TransportUnavailable("Codex app-server websocket closed")
            if opcode == 0x9:
                writer = self._require_writer()
                writer.write(_websocket_control_frame(0xA, payload))
                await writer.drain()
                continue
            if opcode != 0x1:
                continue
            try:
                message = json.loads(payload.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise TransportUnavailable("Codex app-server returned invalid JSON") from exc
            if not isinstance(message, dict):
                raise TransportUnavailable("Codex app-server returned non-object JSON")
            return message
        raise TimeoutError

    def _require_reader(self) -> asyncio.StreamReader:
        if self._reader is None:
            raise TransportUnavailable("Codex app-server websocket is not connected")
        return self._reader

    def _require_writer(self) -> asyncio.StreamWriter:
        if self._writer is None or self._writer.is_closing():
            raise TransportUnavailable("Codex app-server websocket is not connected")
        return self._writer

    def _close_websocket(self) -> None:
        if self._writer is not None:
            self._writer.close()
        self._reader = None
        self._writer = None


async def _wait_closed_safely(writer: asyncio.StreamWriter) -> None:
    try:
        await writer.wait_closed()
    except Exception:
        return


async def _read_websocket_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    first, second = await reader.readexactly(2)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4) if masked else b""
    payload = await reader.readexactly(length) if length else b""
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return opcode, payload


def _websocket_text_frame(text: str) -> bytes:
    return _websocket_frame(0x1, text.encode("utf-8"), masked=True)


def _websocket_control_frame(opcode: int, payload: bytes = b"") -> bytes:
    return _websocket_frame(opcode, payload, masked=True)


def _websocket_frame(opcode: int, payload: bytes, *, masked: bool) -> bytes:
    header = bytearray([0x80 | (opcode & 0x0F)])
    length = len(payload)
    mask_bit = 0x80 if masked else 0
    if length < 126:
        header.append(mask_bit | length)
    elif length < 65536:
        header.append(mask_bit | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(mask_bit | 127)
        header.extend(struct.pack("!Q", length))
    if not masked:
        header.extend(payload)
        return bytes(header)
    mask = secrets.token_bytes(4)
    header.extend(mask)
    header.extend(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes(header)


def _is_response_message(message: dict[str, Any]) -> bool:
    return "result" in message and "id" in message


def _is_error_message(message: dict[str, Any]) -> bool:
    return "error" in message and "id" in message


def _is_notification_message(message: dict[str, Any]) -> bool:
    if "method" in message and "id" not in message:
        return True
    return message.get("type") == "event_msg" and isinstance(message.get("payload"), dict)


_CODEX_HITL_SERVER_REQUEST_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "item/tool/requestUserInput",
    "mcpServer/elicitation/request",
}


def _is_codex_hitl_server_request_message(message: dict[str, Any]) -> bool:
    return (
        "id" in message
        and "method" in message
        and "result" not in message
        and "error" not in message
        and str(message.get("method", "")) in _CODEX_HITL_SERVER_REQUEST_METHODS
    )


def _notification_method(message: dict[str, Any]) -> str:
    method = str(message.get("method", ""))
    if method:
        return method
    if message.get("type") == "event_msg":
        payload = message.get("payload", {})
        if isinstance(payload, dict):
            event_type = str(payload.get("type", "") or "")
            if event_type == "task_complete":
                return "turn/completed"
            return f"event_msg/{event_type}" if event_type else "event_msg"
    return ""


def _notification_thread_id(message: dict[str, Any]) -> str:
    params = message.get("params", {})
    if isinstance(params, dict):
        # `params` is present but empty on the event_msg shape, so returning
        # here unconditionally (the original) made the payload branch below
        # dead code and every event_msg look thread-less.
        thread_id = str(params.get("threadId", "") or params.get("thread_id", "") or "")
        if thread_id:
            return thread_id
        # thread/started names its thread as params.thread.id.
        thread = params.get("thread")
        if isinstance(thread, dict) and thread.get("id"):
            return str(thread["id"])
    payload = message.get("payload", {})
    if isinstance(payload, dict):
        return str(payload.get("threadId", "") or payload.get("thread_id", "") or "")
    return ""


def _notification_matches_thread(message: dict[str, Any], thread_id: str) -> bool:
    """Permissive match: a thread-less message belongs to whoever asks.

    Only safe on messages that were ALREADY routed to this thread (the
    dispatcher decided ownership) or when a single thread is live. Do not use
    it to claim messages out of a shared buffer while several threads share
    one app-server process — that is how one session's events surface in
    another's channel.
    """
    notification_thread_id = _notification_thread_id(message)
    return not notification_thread_id or notification_thread_id == thread_id


def _contains_codex_turn_completed(messages: list[dict[str, Any]], thread_id: str) -> bool:
    return any(
        _notification_method(message) == "turn/completed"
        and _notification_matches_thread(message, thread_id)
        for message in messages
    )


def _contains_codex_hitl_server_request(messages: list[dict[str, Any]], thread_id: str) -> bool:
    return any(
        _is_codex_hitl_server_request_message(message)
        and _notification_matches_thread(message, thread_id)
        for message in messages
    )


def _codex_home_path(codex_home: str = "") -> Path:
    if codex_home:
        return Path(codex_home).expanduser()
    return Path.home() / ".codex"
