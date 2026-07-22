"""Session-level persistent listening for ClaudeHeadlessTransport.

Regression suite for the "overnight silence" bug: the old bridged stream ended
at the FIRST ResultMessage, so turns auto-opened by background subagents
(task notifications) were never relayed, AskUserQuestion after the first turn
never became a HITL card, and the worker process hung forever.

The stream is now a session-level listener:

- it survives turn boundaries and keeps relaying later turns;
- it tracks background subagents in a ledger (task_started adds,
  task_notification / task_updated with terminal status removes,
  background_tasks_changed reconciles authoritatively);
- it settles (and closes the worker) only when no turn is open, the ledger is
  empty, no HITL decision is pending, and a quiet grace elapses — or when the
  background-wait ceiling fires with a visible warning;
- injected user-role messages (the CLI's <task-notification> turns) are never
  echoed back as agent text;
- an IDLE session whose worker is still listening reuses it on the next submit
  instead of forking a second --resume process; resume() closes the previous
  worker instead of leaking it.
"""

import asyncio
import time
import unittest

from walkcode.channel_native import (
    ActorRef,
    AgentEventType,
    AuthorizationStore,
    ChannelBinding,
    ChannelCapabilities,
    ClaudeHeadlessTransport,
    DurableOutbox,
    FakeChannelAdapter,
    InteractionStore,
    Orchestrator,
    ResumeSpec,
    SessionRegistry,
    TransportUnavailable,
    TurnInput,
)


# --- Fake claude_agent_sdk surface -----------------------------------------


class _Allow:
    def __init__(self, updated_input=None, updated_permissions=None):
        self.behavior = "allow"
        self.updated_input = updated_input
        self.updated_permissions = updated_permissions


class _Deny:
    def __init__(self, message="", interrupt=False):
        self.behavior = "deny"
        self.message = message
        self.interrupt = interrupt


class _RuleValue:
    def __init__(self, tool_name, rule_content=None):
        self.tool_name = tool_name
        self.rule_content = rule_content


class _PermissionUpdate:
    def __init__(self, type, rules=None, behavior=None, mode=None, directories=None, destination=None):
        self.type = type
        self.rules = rules
        self.behavior = behavior
        self.mode = mode
        self.directories = directories
        self.destination = destination


class _Ctx:
    def __init__(self, tool_use_id, *, suggestions=None):
        self.tool_use_id = tool_use_id
        self.suggestions = suggestions or []


class _Options:
    def __init__(self, **kwargs):
        self.kwargs = dict(kwargs)
        self.can_use_tool = kwargs.get("can_use_tool")


def _make_sdk(client_cls):
    class SDK:
        ClaudeAgentOptions = _Options
        ClaudeSDKClient = client_cls
        PermissionResultAllow = _Allow
        PermissionResultDeny = _Deny
        PermissionUpdate = _PermissionUpdate
        PermissionRuleValue = _RuleValue

    return SDK


_EOF = object()


def _stream_client_class():
    """A persistent fake client: receive_messages reads a test-fed queue."""

    class _StreamClient:
        instances: list = []

        def __init__(self, options=None):
            self.options = options
            self._can_use_tool = options.can_use_tool if options else None
            self._queue: asyncio.Queue = asyncio.Queue()
            self.disconnected = False
            self.queries: list = []
            type(self).instances.append(self)

        async def connect(self, prompt=None):
            return None

        async def query(self, prompt, session_id="default"):
            self.queries.append(prompt)

        def feed(self, message):
            self._queue.put_nowait(message)

        def feed_eof(self):
            self._queue.put_nowait(_EOF)

        async def receive_messages(self):
            while True:
                item = await self._queue.get()
                if item is _EOF:
                    return
                yield item

        async def disconnect(self):
            self.disconnected = True

    _StreamClient.instances = []
    return _StreamClient


# --- SDK message fakes (class names match claude_agent_sdk types) -----------


def _sdk_message(class_name, **attrs):
    return type(class_name, (), attrs)()


def _assistant(text):
    return _sdk_message("AssistantMessage", content=[{"type": "text", "text": text}])


def _result(message="done", session_id="agent-1"):
    return _sdk_message("ResultMessage", result=message, session_id=session_id)


def _user(text):
    return _sdk_message("UserMessage", content=text)


def _task_started(task_id, description=""):
    return _sdk_message("TaskStartedMessage", task_id=task_id, description=description)


def _task_notification(task_id, status):
    return _sdk_message("TaskNotificationMessage", task_id=task_id, status=status)


def _task_updated(task_id, status=None, patch=None):
    return _sdk_message("TaskUpdatedMessage", task_id=task_id, status=status, patch=patch)


def _background_tasks_changed(tasks):
    return _sdk_message("SystemMessage", subtype="background_tasks_changed", data={"tasks": tasks})


async def _consume(transport, handle, collected):
    stream = await transport.events(handle)
    async for event in stream:
        collected.append(event)


async def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def _transport(client_cls, *, grace=0.05, ceiling=30.0):
    return ClaudeHeadlessTransport(
        sdk_loader=lambda: _make_sdk(client_cls),
        settle_grace_seconds=grace,
        background_wait_ceiling_seconds=ceiling,
    )


class PersistentStreamTests(unittest.TestCase):
    def test_stream_survives_first_result_and_relays_background_turns(self):
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            await transport.submit_turn(handle, TurnInput(text="go"), "k1")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            client.feed(_assistant("派出后台任务，稍等"))
            client.feed(_task_started("t1", "深度调研"))
            client.feed(_result("稍等"))
            # Grace elapses several times over, but the ledger is non-empty:
            # the listener must stay on duty.
            await asyncio.sleep(0.2)
            self.assertFalse(consumer.done())
            client.feed(_user("<task-notification><task-id>t1</task-id></task-notification>"))
            client.feed(_assistant("后台结果来了"))
            client.feed(_task_notification("t1", "completed"))
            client.feed(_result("最终计划"))
            await asyncio.wait_for(consumer, timeout=5.0)
            return transport, handle, client, collected

        transport, handle, client, events = asyncio.run(scenario())
        texts = [e.payload.get("text", "") for e in events if e.type == AgentEventType.TURN_DELTA]
        self.assertIn("后台结果来了", texts)
        # The injected task-notification user message must never echo as text.
        self.assertFalse(any("task-notification" in text for text in texts))
        completed = [e for e in events if e.type == AgentEventType.TURN_COMPLETED]
        self.assertEqual(len(completed), 2)
        ledger_beats = [e for e in events if e.type == AgentEventType.BACKGROUND_TASKS]
        self.assertEqual(ledger_beats[0].payload["count"], 1)
        self.assertEqual(ledger_beats[-1].payload["count"], 0)
        # Settled: worker closed and unregistered.
        self.assertTrue(client.disconnected)
        self.assertFalse(transport.handle_is_live(handle.handle_id))

    def test_absorbed_mid_turn_submit_settles_silently_at_ceiling(self):
        # ADR 0059 R2 regression (v0.14.12): a message submitted while the
        # turn is OPEN may be ABSORBED into that running turn by the CLI (one
        # result covers both submits) instead of queuing a steering turn.
        # The conservative counter then leaks a phantom pending, and the
        # ceiling fired a false 1h "已提交的消息…没有得到任何响应" alarm on a
        # healthy idle session (observed live on 4 sessions). The fix keeps
        # the counter conservative (steering safety) but discriminates at the
        # ceiling: an accounted result AFTER the last submit + a whole quiet
        # ceiling window = absorbed → settle silently, no alarm.
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls, grace=0.05, ceiling=0.4)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            await transport.submit_turn(handle, TurnInput(text="first"), "k1")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            client.feed(_assistant("干活中"))
            # Wait until the turn is visibly open (delta relayed), then
            # inject mid-turn; the CLI absorbs it — no second result comes.
            self.assertTrue(
                await _wait_until(
                    lambda: any(e.type == AgentEventType.TURN_DELTA for e in collected)
                )
            )
            await transport.submit_turn(handle, TurnInput(text="补充说明"), "k2")
            # Conservative accounting: the mid-turn submit IS counted (it
            # could equally have been a queued steering turn).
            self.assertEqual(transport._pending_turns.get(handle.handle_id), 2)
            client.feed(_assistant("收到补充"))
            client.feed(_result("done"))
            await asyncio.wait_for(consumer, timeout=5.0)
            return transport, handle, collected

        transport, handle, events = asyncio.run(scenario())
        # The phantom leftover was recognized as absorbed at the ceiling:
        # silent settle — no false alarm, no error event.
        self.assertNotIn(handle.handle_id, transport._pending_turns)
        texts = [e.payload.get("text", "") for e in events if e.type == AgentEventType.TURN_DELTA]
        self.assertFalse(any("没有得到任何响应" in text for text in texts))
        self.assertFalse(any(e.type == AgentEventType.SESSION_ERROR for e in events))
        self.assertFalse(transport.handle_is_live(handle.handle_id))

    def test_absorbed_mid_turn_submit_does_not_report_pending_lost_at_eof(self):
        # Same absorbed-leak scenario, worker EOF flavor: the phantom pending
        # must NOT be reported as pending_turn_lost — that message was already
        # processed, and ADR 0058's auto-replay would RE-EXECUTE it.
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            await transport.submit_turn(handle, TurnInput(text="first"), "k1")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            client.feed(_assistant("干活中"))
            self.assertTrue(
                await _wait_until(
                    lambda: any(e.type == AgentEventType.TURN_DELTA for e in collected)
                )
            )
            await transport.submit_turn(handle, TurnInput(text="补充说明"), "k2")
            client.feed(_result("done"))
            # Wait for the result to be accounted (phantom leftover = 1),
            # then the worker dies.
            self.assertTrue(
                await _wait_until(
                    lambda: transport._pending_turns.get(handle.handle_id) == 1
                )
            )
            client.feed_eof()
            await asyncio.wait_for(consumer, timeout=5.0)
            return transport, handle, collected

        transport, handle, events = asyncio.run(scenario())
        errors = [e for e in events if e.type == AgentEventType.SESSION_ERROR]
        self.assertEqual(errors, [])
        self.assertFalse(transport.handle_is_live(handle.handle_id))

    def test_submits_with_no_open_turn_still_counted_individually(self):
        # Guard for the 2026-07-18 incident semantics: messages submitted
        # while NO turn is open each queue their own turn and must each be
        # accounted, so settle cannot close the worker under a queued turn.
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            await transport.submit_turn(handle, TurnInput(text="a"), "k1")
            await transport.submit_turn(handle, TurnInput(text="b"), "k2")
            return transport, handle

        transport, handle = asyncio.run(scenario())
        self.assertEqual(transport._pending_turns.get(handle.handle_id), 2)

    def test_settles_after_plain_turn_without_background_tasks(self):
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            await transport.submit_turn(handle, TurnInput(text="hi"), "k1")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            client.feed(_assistant("你好"))
            client.feed(_result("你好"))
            await asyncio.wait_for(consumer, timeout=5.0)
            return transport, handle, client

        transport, handle, client = asyncio.run(scenario())
        self.assertTrue(client.disconnected)
        self.assertFalse(transport.handle_is_live(handle.handle_id))

    def test_task_updated_terminal_status_clears_ledger(self):
        # Not every terminating subagent emits task_notification; a terminal
        # task_updated (even status buried in the patch) must also clear it.
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            # Short injection hold: this test asserts ledger accounting only.
            transport._NOTIFICATION_FOLLOWUP_GRACE = 0.1
            await transport.submit_turn(handle, TurnInput(text="go"), "k1")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            client.feed(_task_started("t1", "a"))
            client.feed(_task_started("t2", "b"))
            client.feed(_result("稍等"))
            client.feed(_task_updated("t1", status="failed"))
            client.feed(_task_updated("t2", patch={"status": "killed"}))
            await asyncio.wait_for(consumer, timeout=5.0)
            return collected

        events = asyncio.run(scenario())
        counts = [e.payload["count"] for e in events if e.type == AgentEventType.BACKGROUND_TASKS]
        self.assertEqual(counts, [1, 2, 1, 0])

    def test_revived_task_reenters_ledger_and_notifies_again(self):
        # The same task_id may notify more than once (SendMessage revives a
        # finished subagent): removal is by status, and a running update
        # re-adds it — settle must wait for the SECOND completion.
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            # Keep the injected-turn hold short: this test only asserts ledger
            # accounting, not the injection window (covered elsewhere).
            transport._NOTIFICATION_FOLLOWUP_GRACE = 0.1
            await transport.submit_turn(handle, TurnInput(text="go"), "k1")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            client.feed(_task_started("t1", "研究"))
            client.feed(_result("稍等"))
            client.feed(_task_notification("t1", "completed"))
            client.feed(_task_updated("t1", status="running"))
            await asyncio.sleep(0.2)
            self.assertFalse(consumer.done())
            client.feed(_task_notification("t1", "completed"))
            await asyncio.wait_for(consumer, timeout=5.0)
            return collected

        events = asyncio.run(scenario())
        counts = [e.payload["count"] for e in events if e.type == AgentEventType.BACKGROUND_TASKS]
        self.assertEqual(counts, [1, 0, 1, 0])

    def test_background_tasks_changed_empty_list_reconciles_and_settles(self):
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            # Short injection hold: this test asserts ledger accounting only.
            transport._NOTIFICATION_FOLLOWUP_GRACE = 0.1
            await transport.submit_turn(handle, TurnInput(text="go"), "k1")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            client.feed(_task_started("t1", "a"))
            client.feed(_task_started("t2", "b"))
            client.feed(_result("稍等"))
            # Authoritative reconcile: the CLI says nothing is running anymore.
            client.feed(_background_tasks_changed([]))
            await asyncio.wait_for(consumer, timeout=5.0)
            return collected

        events = asyncio.run(scenario())
        counts = [e.payload["count"] for e in events if e.type == AgentEventType.BACKGROUND_TASKS]
        self.assertEqual(counts[-1], 0)

    def test_ceiling_emits_visible_warning_and_settles(self):
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls, grace=0.05, ceiling=0.2)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            await transport.submit_turn(handle, TurnInput(text="go"), "k1")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            client.feed(_task_started("t1", "永不回来的调研"))
            client.feed(_result("稍等"))
            await asyncio.wait_for(consumer, timeout=5.0)
            return transport, handle, client, collected

        transport, handle, client, events = asyncio.run(scenario())
        texts = [e.payload.get("text", "") for e in events if e.type == AgentEventType.TURN_DELTA]
        self.assertTrue(any("后台任务等待超时" in text for text in texts))
        self.assertTrue(client.disconnected)
        self.assertFalse(transport.handle_is_live(handle.handle_id))

    def test_ask_user_between_turns_floats_and_blocks_settle(self):
        # The 01:21 AskUserQuestion case: the turn already completed, a
        # background task keeps the stream alive, and the SDK invokes
        # can_use_tool from its control-request handler. The ask must float as
        # an event, block settle while pending, and resolve via the transport.
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            await transport.submit_turn(handle, TurnInput(text="go"), "k1")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            client.feed(_task_started("t1", "调研"))
            client.feed(_result("稍等"))
            ask_call = asyncio.create_task(
                client._can_use_tool(
                    "AskUserQuestion",
                    {"questions": [{"question": "要配单杠吗", "options": [{"label": "要"}]}]},
                    _Ctx("ask-1"),
                )
            )
            floated = await _wait_until(
                lambda: any(e.type == AgentEventType.ASK_USER_REQUESTED for e in collected)
            )
            self.assertTrue(floated, "ask event never floated after turn completion")
            await asyncio.sleep(0.2)
            self.assertFalse(consumer.done())
            await transport.answer_user_question(handle, "ask-1", {"0": "要"})
            decision = await asyncio.wait_for(ask_call, timeout=5.0)
            client.feed(_task_notification("t1", "completed"))
            client.feed(_result("完整计划"))
            await asyncio.wait_for(consumer, timeout=5.0)
            return decision, collected

        decision, events = asyncio.run(scenario())
        self.assertEqual(decision.behavior, "allow")
        self.assertEqual(decision.updated_input["answers"], {"要配单杠吗": "要"})
        ask = next(e for e in events if e.type == AgentEventType.ASK_USER_REQUESTED)
        self.assertEqual(ask.payload["rid"], "ask-1")

    def test_worker_eof_settles_and_unregisters(self):
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls, grace=5.0)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            await transport.submit_turn(handle, TurnInput(text="go"), "k1")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            client.feed(_assistant("干活中"))
            client.feed_eof()
            await asyncio.wait_for(consumer, timeout=5.0)
            return transport, handle

        transport, handle = asyncio.run(scenario())
        self.assertFalse(transport.handle_is_live(handle.handle_id))

    def test_submit_to_settled_worker_raises_transport_unavailable(self):
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            await transport._close_handle_client(handle.handle_id)
            with self.assertRaises(TransportUnavailable):
                await transport.submit_turn(handle, TurnInput(text="hi"), "k1")

        asyncio.run(scenario())

    def test_resume_closes_previous_worker_instead_of_leaking(self):
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls)
            first = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            first_client = transport._clients[first.handle_id]
            second = await transport.resume(
                ResumeSpec(cwd="/tmp/p", session_id="s1", resume_ref={"agent_session_id": "agent-1"})
            )
            return transport, first, first_client, second

        transport, first, first_client, second = asyncio.run(scenario())
        self.assertTrue(first_client.disconnected)
        self.assertFalse(transport.handle_is_live(first.handle_id))
        self.assertTrue(transport.handle_is_live(second.handle_id))
        self.assertEqual(len(transport._clients), 1)


class OrchestratorPersistentDrainTests(unittest.TestCase):
    def _channel_caps(self):
        return ChannelCapabilities(
            thread_context=True,
            editable_message=True,
            interactive_message=True,
            interactive_update=True,
            private_callback_ack=True,
            toast_or_ephemeral_notice=True,
            force_reply=True,
            attachment_download=True,
            forum_or_topic=True,
            max_text_chars=4096,
            max_callback_payload_bytes=64,
        )

    def _build(self, client_cls, *, grace=0.05, ceiling=30.0):
        transport = ClaudeHeadlessTransport(
            sdk_loader=lambda: _make_sdk(client_cls),
            settle_grace_seconds=grace,
            background_wait_ceiling_seconds=ceiling,
        )
        channel = FakeChannelAdapter("telegram", self._channel_caps())
        orch = Orchestrator(
            sessions=SessionRegistry(),
            interactions=InteractionStore(),
            outbox=DurableOutbox(),
            channels={"telegram": channel},
            transports={"claude_headless": transport},
            authz=AuthorizationStore(),
            defer_event_drain=True,
        )
        return transport, channel, orch

    def _sent_texts(self, channel):
        texts = []
        for item in channel.sent_views:
            view = item.get("view", {})
            if view.get("type") in {"turn_delta", "turn_completed"}:
                texts.append(str(view.get("text") or view.get("message") or ""))
        return texts

    async def _drains_done(self, orch):
        drains = list(orch._background_event_drains)
        if drains:
            await asyncio.wait_for(asyncio.gather(*drains), timeout=5.0)

    def test_background_turn_output_reaches_channel_while_session_idle(self):
        # The overnight-silence regression, end to end: turn 1 ends with a
        # subagent still running; the later auto-opened turn's output must
        # reach the channel and the session record must track the ledger.
        async def scenario():
            cls = _stream_client_class()
            transport, channel, orch = self._build(cls)
            owner = ActorRef("telegram", "owner", "Owner")
            binding = ChannelBinding("telegram", "bot", "chat", "topic", "root")
            session = await orch.start_session(binding, "claude_headless", "/tmp/p", owner)
            await orch.submit_user_input(
                session.session_id, TurnInput(text="深度调研"), actor=owner, generation=session.generation
            )
            client = transport._clients[session.transport_ref["handle_id"]]
            client.feed(_assistant("三路调研已经在后台跑起来了，稍等"))
            client.feed(_task_started("t1", "增肌调研"))
            client.feed(_result("三路调研已经在后台跑起来了，稍等", session_id="agent-1"))
            await _wait_until(lambda: session.lifecycle_state == "IDLE")
            await _wait_until(lambda: len(session.background_tasks) == 1)
            idle_with_tasks = (session.lifecycle_state, len(session.background_tasks))
            # Background subagent finishes: the CLI injects a notification and
            # opens a new turn.
            client.feed(_user("<task-notification><task-id>t1</task-id></task-notification>"))
            client.feed(_assistant("七路证据全部到齐，下面是完整计划"))
            client.feed(_task_notification("t1", "completed"))
            client.feed(_result("七路证据全部到齐，下面是完整计划", session_id="agent-1"))
            await self._drains_done(orch)
            return channel, session, idle_with_tasks

        channel, session, idle_with_tasks = asyncio.run(scenario())
        self.assertEqual(idle_with_tasks, ("IDLE", 1))
        texts = self._sent_texts(channel)
        self.assertTrue(any("完整计划" in text for text in texts), texts)
        self.assertFalse(any("task-notification" in text for text in texts), texts)
        self.assertEqual(session.background_tasks, [])
        self.assertEqual(session.lifecycle_state, "IDLE")

    def test_second_submit_reuses_live_worker_without_forking(self):
        async def scenario():
            cls = _stream_client_class()
            # Long grace keeps the listener attached between the two submits.
            transport, channel, orch = self._build(cls, grace=5.0)
            owner = ActorRef("telegram", "owner", "Owner")
            binding = ChannelBinding("telegram", "bot", "chat", "topic", "root")
            session = await orch.start_session(binding, "claude_headless", "/tmp/p", owner)
            await orch.submit_user_input(
                session.session_id, TurnInput(text="one"), actor=owner, generation=session.generation
            )
            client = transport._clients[session.transport_ref["handle_id"]]
            client.feed(_assistant("first"))
            client.feed(_result("first", session_id="agent-1"))
            await _wait_until(lambda: session.lifecycle_state == "IDLE")
            first_handle = session.transport_ref["handle_id"]
            result = await orch.submit_user_input(
                session.session_id, TurnInput(text="two"), actor=owner, generation=session.generation
            )
            reused_handle = session.transport_ref["handle_id"]
            client.feed(_assistant("second answer"))
            client.feed(_result("second answer", session_id="agent-1"))
            await _wait_until(
                lambda: any("second answer" in text for text in self._sent_texts(channel))
            )
            client.feed_eof()
            await self._drains_done(orch)
            return cls, result, first_handle, reused_handle, client, channel

        cls, result, first_handle, reused_handle, client, channel = asyncio.run(scenario())
        self.assertTrue(result.accepted)
        self.assertEqual(first_handle, reused_handle)
        # One worker, both prompts through it, exactly one process.
        self.assertEqual(len(cls.instances), 1)
        self.assertEqual(len(client.queries), 2)
        self.assertTrue(any("second answer" in text for text in self._sent_texts(channel)))

    def test_submit_after_settle_falls_back_to_resume(self):
        async def scenario():
            cls = _stream_client_class()
            transport, channel, orch = self._build(cls)
            owner = ActorRef("telegram", "owner", "Owner")
            binding = ChannelBinding("telegram", "bot", "chat", "topic", "root")
            session = await orch.start_session(binding, "claude_headless", "/tmp/p", owner)
            await orch.submit_user_input(
                session.session_id, TurnInput(text="one"), actor=owner, generation=session.generation
            )
            client = transport._clients[session.transport_ref["handle_id"]]
            client.feed(_assistant("first"))
            client.feed(_result("first", session_id="agent-1"))
            # Let the stream settle: turn closed, ledger empty, grace elapses.
            await _wait_until(lambda: not transport._clients)
            first_handle = session.transport_ref["handle_id"]
            result = await orch.submit_user_input(
                session.session_id, TurnInput(text="two"), actor=owner, generation=session.generation
            )
            second_handle = session.transport_ref["handle_id"]
            second_client = transport._clients[second_handle]
            second_client.feed(_assistant("resumed answer"))
            second_client.feed(_result("resumed answer", session_id="agent-1"))
            await self._drains_done(orch)
            return cls, result, first_handle, second_handle, channel

        cls, result, first_handle, second_handle, channel = asyncio.run(scenario())
        self.assertTrue(result.accepted)
        self.assertNotEqual(first_handle, second_handle)
        self.assertEqual(len(cls.instances), 2)
        self.assertTrue(any("resumed answer" in text for text in self._sent_texts(channel)))


class DeepReviewRegressionTests(unittest.TestCase):
    """Fixes adopted from the v0.14.0 deep-review round (all VERIFIED)."""

    def test_dict_user_message_is_not_echoed_as_agent_text(self):
        converted = ClaudeHeadlessTransport._convert_sdk_message(
            {"type": "user", "role": "user", "content": "<task-notification>t1</task-notification>"}
        )
        events = converted if isinstance(converted, list) else ([] if converted is None else [converted])
        self.assertFalse(
            any(e.type == AgentEventType.TURN_DELTA for e in events),
            f"dict user message leaked as agent text: {events}",
        )

    def test_submit_failure_clears_pending_turn_marker(self):
        class _BrokenQueryClient(_stream_client_class()):
            async def query(self, prompt, session_id="default"):
                raise RuntimeError("submit boom")

        async def scenario():
            transport = ClaudeHeadlessTransport(sdk_loader=lambda: _make_sdk(_BrokenQueryClient))
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            with self.assertRaises(RuntimeError):
                await transport.submit_turn(handle, TurnInput(text="hi"), "k1")
            # A failed submit must not leave a "turn in flight" marker that
            # blocks settle forever.
            self.assertNotIn(handle.handle_id, transport._pending_turns)

        asyncio.run(scenario())

    def test_stream_exception_unregisters_broken_worker(self):
        class _ExplodingStreamClient(_stream_client_class()):
            async def receive_messages(self):
                yield _assistant("ok")
                raise RuntimeError("stream boom")

        async def scenario():
            transport = _transport(_ExplodingStreamClient)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            await transport.submit_turn(handle, TurnInput(text="go"), "k1")
            collected: list = []
            with self.assertRaises(RuntimeError):
                await _consume(transport, handle, collected)
            # The broken worker must be unregistered so the next submit
            # resumes a fresh process instead of reusing the dead connection.
            self.assertFalse(transport.handle_is_live(handle.handle_id))

        asyncio.run(scenario())

    def test_worker_eof_with_pending_tasks_warns_and_clears_ledger(self):
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls, grace=5.0)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            await transport.submit_turn(handle, TurnInput(text="go"), "k1")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            client.feed(_task_started("t1", "研究"))
            client.feed(_result("稍等"))
            client.feed_eof()
            await asyncio.wait_for(consumer, timeout=5.0)
            return transport, handle, collected

        transport, handle, events = asyncio.run(scenario())
        texts = [e.payload.get("text", "") for e in events if e.type == AgentEventType.TURN_DELTA]
        self.assertTrue(any("后台任务未完成" in text for text in texts), texts)
        ledger = [e for e in events if e.type == AgentEventType.BACKGROUND_TASKS]
        self.assertEqual(ledger[-1].payload["count"], 0)
        self.assertEqual(ledger[-1].payload.get("abandoned"), 1)
        self.assertFalse(transport.handle_is_live(handle.handle_id))

    def test_notification_before_slow_injected_turn_does_not_settle(self):
        # A terminal task_notification empties the ledger, but the CLI's
        # injected follow-up turn may land later than the settle grace; the
        # listener must hold on for the bounded injection window.
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls, grace=0.05)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            transport._NOTIFICATION_FOLLOWUP_GRACE = 1.0
            await transport.submit_turn(handle, TurnInput(text="go"), "k1")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            client.feed(_task_started("t1", "研究"))
            client.feed(_result("稍等"))
            client.feed(_task_notification("t1", "completed"))
            # Well past the settle grace, still inside the injection window.
            await asyncio.sleep(0.4)
            self.assertFalse(consumer.done(), "listener settled before the injected turn arrived")
            client.feed(_user("<task-notification><task-id>t1</task-id></task-notification>"))
            client.feed(_assistant("迟到的完整计划"))
            client.feed(_result("迟到的完整计划"))
            await asyncio.wait_for(consumer, timeout=5.0)
            return collected

        events = asyncio.run(scenario())
        texts = [e.payload.get("text", "") for e in events if e.type == AgentEventType.TURN_DELTA]
        self.assertIn("迟到的完整计划", texts)

    def test_settle_race_submit_falls_back_to_resume(self):
        # handle_supports_reuse() lies (simulating the check passing just
        # before the listener detached the handle): submit_turn must raise
        # TransportUnavailable and the orchestrator must recover via resume.
        class _RacingTransport(ClaudeHeadlessTransport):
            def handle_supports_reuse(self, handle_id):
                return True

        async def scenario():
            cls = _stream_client_class()
            transport = _RacingTransport(
                sdk_loader=lambda: _make_sdk(cls),
                settle_grace_seconds=0.05,
                background_wait_ceiling_seconds=30.0,
            )
            channel = FakeChannelAdapter(
                "telegram",
                ChannelCapabilities(
                    thread_context=True,
                    editable_message=True,
                    interactive_message=True,
                    interactive_update=True,
                    private_callback_ack=True,
                    toast_or_ephemeral_notice=True,
                    force_reply=True,
                    attachment_download=True,
                    forum_or_topic=True,
                    max_text_chars=4096,
                    max_callback_payload_bytes=64,
                ),
            )
            orch = Orchestrator(
                sessions=SessionRegistry(),
                interactions=InteractionStore(),
                outbox=DurableOutbox(),
                channels={"telegram": channel},
                transports={"claude_headless": transport},
                authz=AuthorizationStore(),
                defer_event_drain=True,
            )
            owner = ActorRef("telegram", "owner", "Owner")
            binding = ChannelBinding("telegram", "bot", "chat", "topic", "root")
            session = await orch.start_session(binding, "claude_headless", "/tmp/p", owner)
            await orch.submit_user_input(
                session.session_id, TurnInput(text="one"), actor=owner, generation=session.generation
            )
            client = transport._clients[session.transport_ref["handle_id"]]
            client.feed(_assistant("first"))
            client.feed(_result("first", session_id="agent-1"))
            # Let the listener settle so the old handle is really gone while
            # handle_is_live keeps claiming it is alive.
            await _wait_until(lambda: not transport._clients)
            result = await orch.submit_user_input(
                session.session_id, TurnInput(text="two"), actor=owner, generation=session.generation
            )
            new_client = transport._clients[session.transport_ref["handle_id"]]
            new_client.feed(_assistant("recovered"))
            new_client.feed(_result("recovered", session_id="agent-1"))
            drains = list(orch._background_event_drains)
            if drains:
                await asyncio.wait_for(asyncio.gather(*drains), timeout=5.0)
            return cls, result

        cls, result = asyncio.run(scenario())
        self.assertTrue(result.accepted)
        self.assertEqual(len(cls.instances), 2)

    def test_close_session_marks_stopped_with_sdk_shaped_client(self):
        # The real SDK client has no shutdown() control method; closing the
        # session must still succeed and mark it stopped.
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls, grace=5.0)
            channel = FakeChannelAdapter(
                "telegram",
                ChannelCapabilities(
                    thread_context=True,
                    editable_message=True,
                    interactive_message=True,
                    interactive_update=True,
                    private_callback_ack=True,
                    toast_or_ephemeral_notice=True,
                    force_reply=True,
                    attachment_download=True,
                    forum_or_topic=True,
                    max_text_chars=4096,
                    max_callback_payload_bytes=64,
                ),
            )
            orch = Orchestrator(
                sessions=SessionRegistry(),
                interactions=InteractionStore(),
                outbox=DurableOutbox(),
                channels={"telegram": channel},
                transports={"claude_headless": transport},
                defer_event_drain=True,
            )
            owner = ActorRef("telegram", "owner", "Owner")
            binding = ChannelBinding("telegram", "bot", "chat", "topic", "root")
            session = await orch.start_session(binding, "claude_headless", "/tmp/p", owner)
            await orch.submit_user_input(
                session.session_id, TurnInput(text="one"), actor=owner, generation=session.generation
            )
            client = transport._clients[session.transport_ref["handle_id"]]
            client.feed(_assistant("hi"))
            client.feed(_result("hi", session_id="agent-1"))
            await _wait_until(lambda: session.lifecycle_state == "IDLE")
            result = await orch.close_session(
                session.session_id, actor=owner, reason="user_requested"
            )
            client.feed_eof()
            drains = list(orch._background_event_drains)
            if drains:
                await asyncio.wait_for(asyncio.gather(*drains), timeout=5.0)
            return result, session, client

        result, session, client = asyncio.run(scenario())
        self.assertTrue(result.accepted)
        self.assertEqual(session.status, "stopped")
        self.assertEqual(session.lifecycle_state, "STOPPED")
        self.assertTrue(client.disconnected)

    def test_bare_terminal_task_updated_holds_for_injected_turn(self):
        # Round-2 Critical: a terminal task_updated (no task_notification at
        # all) drains the ledger between turns; settle must still wait the
        # injection window for the CLI's follow-up turn.
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls, grace=0.05)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            transport._NOTIFICATION_FOLLOWUP_GRACE = 1.0
            await transport.submit_turn(handle, TurnInput(text="go"), "k1")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            client.feed(_task_started("t1", "研究"))
            client.feed(_result("稍等"))
            client.feed(_task_updated("t1", status="completed"))
            await asyncio.sleep(0.4)
            self.assertFalse(consumer.done(), "settled before the injected turn despite ledger drain")
            client.feed(_assistant("task_updated 之后的完整计划"))
            client.feed(_result("task_updated 之后的完整计划"))
            await asyncio.wait_for(consumer, timeout=5.0)
            return collected

        events = asyncio.run(scenario())
        texts = [e.payload.get("text", "") for e in events if e.type == AgentEventType.TURN_DELTA]
        self.assertIn("task_updated 之后的完整计划", texts)

    def test_statusless_notification_clears_ledger(self):
        # A task_notification without a status field still means "this agent
        # stopped": the ledger entry must clear instead of waiting for the
        # ceiling to fire a false alarm.
        active: dict = {}
        is_task, changed, subtype = ClaudeHeadlessTransport._apply_task_message(
            _task_started("t1", "研究"), active
        )
        self.assertEqual((is_task, changed, subtype), (True, True, "task_started"))
        is_task, changed, subtype = ClaudeHeadlessTransport._apply_task_message(
            _sdk_message("TaskNotificationMessage", task_id="t1", status=""), active
        )
        self.assertEqual((is_task, changed, subtype), (True, True, "task_notification"))
        self.assertEqual(active, {})

    def test_legacy_single_turn_client_second_submit_resumes(self):
        # A legacy client (receive_response only, no receive_messages) has no
        # session-level listener: the second submit must resume a fresh worker
        # instead of reusing the stale one.
        class _LegacyClient:
            instances: list = []

            def __init__(self, options=None):
                self.options = options
                self.queries: list = []
                type(self).instances.append(self)

            async def connect(self, prompt=None):
                return None

            async def query(self, prompt, session_id="default"):
                self.queries.append(prompt)

            async def receive_response(self):
                yield {"content": [{"type": "text", "text": "legacy reply"}]}
                yield {"type": "result", "result": "legacy reply", "session_id": "agent-legacy"}

        _LegacyClient.instances = []

        async def scenario():
            transport = ClaudeHeadlessTransport(sdk_loader=lambda: _make_sdk(_LegacyClient))
            channel = FakeChannelAdapter(
                "telegram",
                ChannelCapabilities(
                    thread_context=True,
                    editable_message=True,
                    interactive_message=True,
                    interactive_update=True,
                    private_callback_ack=True,
                    toast_or_ephemeral_notice=True,
                    force_reply=True,
                    attachment_download=True,
                    forum_or_topic=True,
                    max_text_chars=4096,
                    max_callback_payload_bytes=64,
                ),
            )
            orch = Orchestrator(
                sessions=SessionRegistry(),
                interactions=InteractionStore(),
                outbox=DurableOutbox(),
                channels={"telegram": channel},
                transports={"claude_headless": transport},
                authz=AuthorizationStore(),
                defer_event_drain=True,
            )
            owner = ActorRef("telegram", "owner", "Owner")
            binding = ChannelBinding("telegram", "bot", "chat", "topic", "root")
            session = await orch.start_session(binding, "claude_headless", "/tmp/p", owner)
            await orch.submit_user_input(
                session.session_id, TurnInput(text="one"), actor=owner, generation=session.generation
            )
            drains = list(orch._background_event_drains)
            if drains:
                await asyncio.wait_for(asyncio.gather(*drains), timeout=5.0)
            await orch.submit_user_input(
                session.session_id, TurnInput(text="two"), actor=owner, generation=session.generation
            )
            drains = list(orch._background_event_drains)
            if drains:
                await asyncio.wait_for(asyncio.gather(*drains), timeout=5.0)
            return _LegacyClient.instances

        instances = asyncio.run(scenario())
        self.assertEqual(len(instances), 2, "legacy client must be resumed, not reused")

    def test_ceiling_leaves_session_idle_not_error(self):
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls, grace=0.05, ceiling=0.3)
            channel = FakeChannelAdapter(
                "telegram",
                ChannelCapabilities(
                    thread_context=True,
                    editable_message=True,
                    interactive_message=True,
                    interactive_update=True,
                    private_callback_ack=True,
                    toast_or_ephemeral_notice=True,
                    force_reply=True,
                    attachment_download=True,
                    forum_or_topic=True,
                    max_text_chars=4096,
                    max_callback_payload_bytes=64,
                ),
            )
            orch = Orchestrator(
                sessions=SessionRegistry(),
                interactions=InteractionStore(),
                outbox=DurableOutbox(),
                channels={"telegram": channel},
                transports={"claude_headless": transport},
                authz=AuthorizationStore(),
                defer_event_drain=True,
            )
            owner = ActorRef("telegram", "owner", "Owner")
            binding = ChannelBinding("telegram", "bot", "chat", "topic", "root")
            session = await orch.start_session(binding, "claude_headless", "/tmp/p", owner)
            await orch.submit_user_input(
                session.session_id, TurnInput(text="go"), actor=owner, generation=session.generation
            )
            client = transport._clients[session.transport_ref["handle_id"]]
            client.feed(_assistant("稍等"))
            client.feed(_task_started("t1", "永不回来的调研"))
            client.feed(_result("稍等", session_id="agent-1"))
            drains = list(orch._background_event_drains)
            if drains:
                await asyncio.wait_for(asyncio.gather(*drains), timeout=10.0)
            texts = []
            for item in channel.sent_views:
                view = item.get("view", {})
                if view.get("type") in {"turn_delta", "turn_completed"}:
                    texts.append(str(view.get("text") or view.get("message") or ""))
            return session, texts

        session, texts = asyncio.run(scenario())
        # The ceiling is a deliberate settle, not a broken stream: the session
        # must end IDLE with an empty ledger and a visible warning.
        self.assertEqual(session.lifecycle_state, "IDLE")
        self.assertEqual(session.background_tasks, [])
        self.assertTrue(any("后台任务等待超时" in text for text in texts), texts)


class TakeoverInjectedTurnRegressionTests(unittest.TestCase):
    """2026-07-18 live incident: after a takeover, the resume fork auto-runs a
    stopped-task notification turn FIRST; its result must not account for the
    user's submitted turn, or settle kills the worker while that turn is still
    silently queued (the user's "yes" never got a reply)."""

    def test_injected_turn_result_does_not_consume_submit_marker(self):
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls, grace=0.05)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            # Takeover ordering: the blocked input is submitted BEFORE the
            # listener attaches (defer_event_drain).
            await transport.submit_turn(handle, TurnInput(text="yes"), "k1")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            # The CLI runs the injected notification turn first: opening
            # traffic is a stream user message, then its result lands.
            client.feed(_user("<task-notification><task-id>t9</task-id><status>stopped</status></task-notification>"))
            client.feed(_result("旧回合收尾", session_id="agent-1"))
            # Well past the grace: the submitted "yes" turn has produced no
            # stream traffic yet, but the listener must stay on duty.
            await asyncio.sleep(0.4)
            self.assertFalse(
                consumer.done(),
                "settle fired while the submitted turn was still queued (incident regression)",
            )
            client.feed(_assistant("yes 的真正回复"))
            client.feed(_result("yes 的真正回复", session_id="agent-1"))
            await asyncio.wait_for(consumer, timeout=5.0)
            return transport, handle, client, collected

        transport, handle, client, events = asyncio.run(scenario())
        texts = [e.payload.get("text", "") for e in events if e.type == AgentEventType.TURN_DELTA]
        self.assertIn("yes 的真正回复", texts)
        # After the REAL submitted turn completed, settle proceeds normally.
        self.assertTrue(client.disconnected)
        self.assertFalse(transport.handle_is_live(handle.handle_id))

    def test_typed_notification_with_bare_result_does_not_consume_submit(self):
        # Review round G2: the stopped-task notification can arrive as a typed
        # TaskNotificationMessage (no user-role opening traffic) followed by a
        # bare result; that result must not account for the queued submit.
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls, grace=0.05)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            transport._NOTIFICATION_FOLLOWUP_GRACE = 0.1
            await transport.submit_turn(handle, TurnInput(text="yes"), "k1")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            client.feed(_task_notification("t9", "stopped"))
            client.feed(_result("旧回合收尾", session_id="agent-1"))
            await asyncio.sleep(0.4)
            self.assertFalse(consumer.done(), "typed notification result consumed the queued submit")
            client.feed(_assistant("yes 的真正回复"))
            client.feed(_result("yes 的真正回复", session_id="agent-1"))
            await asyncio.wait_for(consumer, timeout=5.0)
            return collected

        events = asyncio.run(scenario())
        texts = [e.payload.get("text", "") for e in events if e.type == AgentEventType.TURN_DELTA]
        self.assertIn("yes 的真正回复", texts)

    def test_result_only_turn_on_reused_worker_still_settles(self):
        # Review round G3a: a reused listener gets a second submit whose turn
        # emits ONLY a result (no assistant/user traffic first). The counter
        # must still be consumed — a stuck marker would hold the worker open
        # forever.
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls, grace=0.05)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            await transport.submit_turn(handle, TurnInput(text="one"), "k1")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            client.feed(_assistant("first"))
            client.feed(_result("first", session_id="agent-1"))
            await _wait_until(lambda: any(e.type == AgentEventType.TURN_COMPLETED for e in collected))
            # Reuse: second submit, then a bare result with no other traffic.
            await transport.submit_turn(handle, TurnInput(text="two"), "k2")
            client.feed(_result("second (result only)", session_id="agent-1"))
            await asyncio.wait_for(consumer, timeout=5.0)
            return transport, handle

        transport, handle = asyncio.run(scenario())
        self.assertFalse(transport.handle_is_live(handle.handle_id))

    def test_two_queued_submits_need_two_results(self):
        # Review round G3b: two user messages queued back-to-back — the first
        # reply must not settle the listener while the second is still queued.
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls, grace=0.05)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            await transport.submit_turn(handle, TurnInput(text="one"), "k1")
            await transport.submit_turn(handle, TurnInput(text="two"), "k2")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            client.feed(_assistant("回复一"))
            client.feed(_result("回复一", session_id="agent-1"))
            await asyncio.sleep(0.3)
            self.assertFalse(consumer.done(), "settled with the second submit still queued")
            client.feed(_assistant("回复二"))
            client.feed(_result("回复二", session_id="agent-1"))
            await asyncio.wait_for(consumer, timeout=5.0)
            return collected

        events = asyncio.run(scenario())
        texts = [e.payload.get("text", "") for e in events if e.type == AgentEventType.TURN_DELTA]
        self.assertIn("回复二", texts)

    def test_unanswered_submit_hits_ceiling_with_visible_warning(self):
        # A submit that never produces any stream traffic must not hold the
        # worker open forever: past the ceiling the listener warns and closes.
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls, grace=0.05, ceiling=0.3)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            await transport.submit_turn(handle, TurnInput(text="one"), "k1")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            client.feed(_assistant("first"))
            client.feed(_result("first", session_id="agent-1"))
            await _wait_until(lambda: any(e.type == AgentEventType.TURN_COMPLETED for e in collected))
            # Second submit gets NO response at all.
            await transport.submit_turn(handle, TurnInput(text="two"), "k2")
            await asyncio.wait_for(consumer, timeout=5.0)
            return transport, handle, collected

        transport, handle, events = asyncio.run(scenario())
        texts = [e.payload.get("text", "") for e in events if e.type == AgentEventType.TURN_DELTA]
        self.assertTrue(any("没有得到任何响应" in text for text in texts), texts)
        self.assertFalse(transport.handle_is_live(handle.handle_id))

    def test_injected_turn_with_assistant_text_does_not_consume_submit(self):
        # Round-2 review Critical: the notification-predicted injected turn
        # usually streams assistant text; that must not reclassify it as a
        # user turn whose result consumes the queued submit.
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls, grace=0.05)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            transport._NOTIFICATION_FOLLOWUP_GRACE = 1.0
            await transport.submit_turn(handle, TurnInput(text="yes"), "k1")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            client.feed(_task_notification("t9", "stopped"))
            client.feed(_assistant("注入回合的正文"))
            client.feed(_result("注入回合的正文", session_id="agent-1"))
            await asyncio.sleep(0.4)
            self.assertFalse(consumer.done(), "injected turn with text consumed the queued submit")
            client.feed(_assistant("yes 的真正回复"))
            client.feed(_result("yes 的真正回复", session_id="agent-1"))
            await asyncio.wait_for(consumer, timeout=5.0)
            return collected

        events = asyncio.run(scenario())
        texts = [e.payload.get("text", "") for e in events if e.type == AgentEventType.TURN_DELTA]
        self.assertIn("yes 的真正回复", texts)

    def test_fresh_submit_gets_full_pending_window_despite_quiet_background(self):
        # Round-2 review Critical: the pending-turn ceiling must clock from
        # the submit, not from the last stream traffic — a fresh submit after
        # a long-quiet background task must not be killed instantly.
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls, grace=0.05, ceiling=0.4)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            await transport.submit_turn(handle, TurnInput(text="one"), "k1")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            client.feed(_assistant("first"))
            client.feed(_task_started("t1", "长静默任务"))
            client.feed(_result("first", session_id="agent-1"))
            # Background quiet for most of the ceiling window.
            await asyncio.sleep(0.3)
            self.assertFalse(consumer.done())
            await transport.submit_turn(handle, TurnInput(text="two"), "k2")
            # Under the bug the inherited quiet clock kills this submit almost
            # immediately; with the fix it gets its own full window.
            await asyncio.sleep(0.2)
            self.assertFalse(consumer.done(), "fresh submit was killed by inherited quiet clock")
            client.feed(_assistant("second reply"))
            client.feed(_task_notification("t1", "completed"))
            client.feed(_result("second reply", session_id="agent-1"))
            await asyncio.wait_for(consumer, timeout=5.0)
            return collected

        events = asyncio.run(scenario())
        texts = [e.payload.get("text", "") for e in events if e.type == AgentEventType.TURN_DELTA]
        self.assertIn("second reply", texts)

    def test_bare_result_after_expired_injection_window_consumes_submit(self):
        # Residual from the sticky prediction: if the predicted injected turn
        # never comes, a later bare-result user turn must still settle.
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls, grace=0.05)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            transport._NOTIFICATION_FOLLOWUP_GRACE = 0.2
            await transport.submit_turn(handle, TurnInput(text="one"), "k1")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            client.feed(_task_notification("t1", "stopped"))
            # Let the injection window lapse with no injected turn.
            await asyncio.sleep(0.4)
            client.feed(_result("bare result of the real turn", session_id="agent-1"))
            await asyncio.wait_for(consumer, timeout=5.0)
            return transport, handle

        transport, handle = asyncio.run(scenario())
        self.assertFalse(transport.handle_is_live(handle.handle_id))

    def test_worker_death_before_any_traffic_yields_visible_error(self):
        # Round-3 review (6-dimension consensus): a submit accepted and then
        # the worker dies before producing a single stream message — that
        # must surface as a SESSION_ERROR, not silence + ACTIVE forever.
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls, grace=0.05)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            await transport.submit_turn(handle, TurnInput(text="hi"), "k1")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            client.feed_eof()
            await asyncio.wait_for(consumer, timeout=5.0)
            return transport, handle, collected

        transport, handle, events = asyncio.run(scenario())
        errors = [e for e in events if e.type == AgentEventType.SESSION_ERROR]
        self.assertTrue(errors, "worker death with a pending submit was silent")
        self.assertIn("没有被处理", str(errors[-1].payload.get("message", "")))
        # ADR 0058 contract pin: the orchestrator keys auto-replay off these
        # structured fields — a renamed/dropped field would silently disable
        # replay while every text-based assert still passes (review R1
        # tests#2).
        self.assertEqual(errors[-1].payload.get("reason"), "pending_turn_lost")
        self.assertIs(errors[-1].payload.get("traffic_seen"), False)
        self.assertEqual(errors[-1].payload.get("pending_lost"), 1)
        self.assertFalse(transport.handle_is_live(handle.handle_id))

    def test_worker_death_after_traffic_marks_traffic_seen(self):
        # ADR 0058: a turn that already streamed output may have executed
        # side effects — the EOF marker must say so, so the orchestrator
        # refuses auto-replay instead of re-executing them (review R1
        # errors/data/risk consensus).
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls, grace=0.05)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            await transport.submit_turn(handle, TurnInput(text="hi"), "k1")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            client.feed(_assistant("partial output, then death"))
            await asyncio.sleep(0.05)
            client.feed_eof()
            await asyncio.wait_for(consumer, timeout=5.0)
            return collected

        events = asyncio.run(scenario())
        errors = [e for e in events if e.type == AgentEventType.SESSION_ERROR]
        self.assertTrue(errors, "mid-turn worker death was silent")
        self.assertEqual(errors[-1].payload.get("reason"), "pending_turn_lost")
        self.assertIs(errors[-1].payload.get("traffic_seen"), True)

    def test_injected_turn_traffic_does_not_mark_queued_submit_executed(self):
        # Review R2 (cross-dimension repro): an INJECTED turn's output must
        # not count as the queued user submit's traffic — the user's message
        # never started, refusing its replay on this evidence is wrong.
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls, grace=0.05)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            await transport.submit_turn(handle, TurnInput(text="queued"), "k1")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            # A user-role stream message opens an injected turn (submitted
            # prompts are never echoed back on the stream)...
            client.feed({"type": "user", "role": "user", "content": "<task-notification>t</task-notification>"})
            await asyncio.sleep(0.02)
            # ...and its assistant output is injected-turn traffic.
            client.feed(_assistant("injected turn output"))
            await asyncio.sleep(0.02)
            client.feed_eof()
            await asyncio.wait_for(consumer, timeout=5.0)
            return collected

        events = asyncio.run(scenario())
        errors = [e for e in events if e.type == AgentEventType.SESSION_ERROR]
        self.assertTrue(errors, "worker death with a queued submit was silent")
        self.assertEqual(errors[-1].payload.get("reason"), "pending_turn_lost")
        self.assertIs(
            errors[-1].payload.get("traffic_seen"),
            False,
            "injected-turn traffic must not mark the queued submit as executed",
        )

    def test_reply_with_text_after_expired_prediction_consumes_submit(self):
        # Round-3 review Critical: prediction expiry must be evaluated at
        # turn-classification time (the pending wait sleeps on the ceiling and
        # never wakes at window expiry).
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls, grace=0.05)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            transport._NOTIFICATION_FOLLOWUP_GRACE = 0.2
            await transport.submit_turn(handle, TurnInput(text="one"), "k1")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            client.feed(_task_notification("t1", "stopped"))
            # Window lapses with no injected turn; then the REAL reply opens
            # with assistant text.
            await asyncio.sleep(0.4)
            client.feed(_assistant("real reply"))
            client.feed(_result("real reply", session_id="agent-1"))
            await asyncio.wait_for(consumer, timeout=5.0)
            return transport, handle

        transport, handle = asyncio.run(scenario())
        self.assertFalse(transport.handle_is_live(handle.handle_id))

    def test_legacy_zero_traffic_eof_with_pending_submit_yields_error(self):
        # Adversarial-verify residual: the legacy (receive_response-only)
        # path must also surface a lost submit instead of ending silently.
        class _DeadLegacyClient:
            def __init__(self, options=None):
                self.options = options

            async def connect(self, prompt=None):
                return None

            async def query(self, prompt, session_id="default"):
                return None

            async def receive_response(self):
                return
                yield  # pragma: no cover — makes this an empty async generator

        async def scenario():
            transport = ClaudeHeadlessTransport(sdk_loader=lambda: _make_sdk(_DeadLegacyClient))
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            await transport.submit_turn(handle, TurnInput(text="hi"), "k1")
            collected: list = []
            await _consume(transport, handle, collected)
            return collected

        events = asyncio.run(scenario())
        errors = [e for e in events if e.type == AgentEventType.SESSION_ERROR]
        self.assertTrue(errors, "legacy zero-traffic EOF with a pending submit was silent")

    def test_internal_typeerror_from_control_method_is_not_swallowed(self):
        # Signature binding (not try/except) decides the call shape: a
        # TypeError raised INSIDE the method must propagate, not trigger a
        # silent second bare invocation.
        class _BuggyInterruptClient(_stream_client_class()):
            def __init__(self, options=None):
                super().__init__(options)
                self.calls = 0

            async def interrupt(self, reason):
                self.calls += 1
                raise TypeError("internal bug")

        async def scenario():
            transport = _transport(_BuggyInterruptClient)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            client = transport._clients[handle.handle_id]
            with self.assertRaises(TypeError):
                await transport.interrupt(handle, "user_requested")
            return client

        client = asyncio.run(scenario())
        self.assertEqual(client.calls, 1, "internal TypeError triggered a hidden retry")

    def test_interrupt_tolerates_argless_sdk_signature(self):
        # The real SDK's interrupt() takes no arguments; forwarding a reason
        # must not fail the control call.
        class _ArglessInterruptClient(_stream_client_class()):
            def __init__(self, options=None):
                super().__init__(options)
                self.interrupted = False

            async def interrupt(self):
                self.interrupted = True

        async def scenario():
            transport = _transport(_ArglessInterruptClient)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            client = transport._clients[handle.handle_id]
            result = await transport.interrupt(handle, "user_requested")
            return result, client

        result, client = asyncio.run(scenario())
        self.assertTrue(result.accepted)
        self.assertTrue(client.interrupted)

    def test_mid_turn_steering_submit_survives_current_turn_result(self):
        # A submit issued while a turn is streaming (queued steering input):
        # the CURRENT turn's result predates the submit and must not consume
        # its marker either.
        async def scenario():
            cls = _stream_client_class()
            transport = _transport(cls, grace=0.05)
            handle = await transport.launch_session(cwd="/tmp/p", session_id="s1")
            await transport.submit_turn(handle, TurnInput(text="one"), "k1")
            client = transport._clients[handle.handle_id]
            collected: list = []
            consumer = asyncio.create_task(_consume(transport, handle, collected))
            client.feed(_assistant("第一回合进行中"))
            await _wait_until(lambda: len(collected) >= 1)
            # Steering submit while turn one is still open.
            await transport.submit_turn(handle, TurnInput(text="two"), "k2")
            client.feed(_result("第一回合结果", session_id="agent-1"))
            await asyncio.sleep(0.3)
            self.assertFalse(consumer.done(), "settle fired while the steering turn was queued")
            client.feed(_assistant("第二回合回复"))
            client.feed(_result("第二回合回复", session_id="agent-1"))
            await asyncio.wait_for(consumer, timeout=5.0)
            return collected

        events = asyncio.run(scenario())
        texts = [e.payload.get("text", "") for e in events if e.type == AgentEventType.TURN_DELTA]
        self.assertIn("第二回合回复", texts)


class HeadlessSettleConfigTests(unittest.TestCase):
    def test_settle_and_ceiling_env_keys_parse(self):
        from walkcode.channel_native import _configured_agent_options

        options = _configured_agent_options(
            {
                "WALKCODE_CLAUDE_SETTLE_GRACE": "2.5",
                "WALKCODE_CLAUDE_BG_WAIT_CEILING": "600",
            }
        )
        self.assertEqual(options["claude"]["settle_grace_seconds"], 2.5)
        self.assertEqual(options["claude"]["background_wait_ceiling_seconds"], 600.0)

    def test_invalid_settle_values_raise(self):
        from walkcode.channel_native import ChannelConfigError, _configured_agent_options

        with self.assertRaises(ChannelConfigError):
            _configured_agent_options({"WALKCODE_CLAUDE_SETTLE_GRACE": "-1"})
        with self.assertRaises(ChannelConfigError):
            _configured_agent_options({"WALKCODE_CLAUDE_BG_WAIT_CEILING": "abc"})


if __name__ == "__main__":
    unittest.main()
