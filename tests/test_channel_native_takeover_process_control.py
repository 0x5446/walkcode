import asyncio
import subprocess
import sys
import unittest
import unittest.mock

from walkcode import channel_native as channel_native_module
from walkcode.channel_native import (
    ActorRef,
    AuthorizationStore,
    ChannelBinding,
    ChannelCapabilities,
    ControlResult,
    DurableOutbox,
    FakeAgentTransport,
    FakeChannelAdapter,
    FakeExternalTuiController,
    InboundEvent,
    InteractionStore,
    LocalProcessController,
    Orchestrator,
    ResumeSpec,
    SessionRegistry,
    SessionRole,
    TakeoverPhase,
    TransportCapabilities,
    TransportHandle,
    TurnInput,
)


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _actor(actor_id: str = "owner") -> ActorRef:
    return ActorRef(channel_kind="lark", actor_id=actor_id, display_name=actor_id.title())


def _binding() -> ChannelBinding:
    return ChannelBinding("lark", "bot", "chat", "topic", "root")


def _channel_caps() -> ChannelCapabilities:
    return ChannelCapabilities(
        editable_message=True,
        private_callback_ack=True,
        attachment_download=True,
    )


def _transport_caps() -> TransportCapabilities:
    return TransportCapabilities(
        structured_input=True,
        structured_output=True,
        permission_callback=True,
        ask_user_question=True,
        set_model=True,
        resume_after_complete=True,
        external_tui_takeover=True,
    )


def _callback(token: str, *, event_id: str = "cb-1") -> InboundEvent:
    return InboundEvent(
        event_id=event_id,
        channel_kind="lark",
        account_id="bot",
        chat_id="chat",
        thread_id="topic",
        message_id="m-cb",
        root_message_id="root",
        sender_id="owner",
        sender_display="Owner",
        text=f"cb:{token}",
        callback={"token": token},
    )


def _action_token(channel: FakeChannelAdapter, action: str) -> str:
    view = channel.sent_views[-1]["view"]
    return next(item["token"] for item in view["actions"] if item["action"] == action)


def _setup(*, terminate_ref=None, controller=None, transport=None, agent_session_id=""):
    clock = _Clock()
    sessions = SessionRegistry(now=clock)
    interactions = InteractionStore(now=clock)
    outbox = DurableOutbox(now=clock)
    authz = AuthorizationStore()
    channel = FakeChannelAdapter("lark", _channel_caps())
    transport = transport or FakeAgentTransport("fake-transport", _transport_caps())
    controller = controller if controller is not None else FakeExternalTuiController("fake-process")
    controllers = {controller.kind: controller} if controller is not None else {}
    orchestrator = Orchestrator(
        sessions=sessions,
        interactions=interactions,
        outbox=outbox,
        channels={"lark": channel},
        transports={"fake-transport": transport},
        external_tui_controllers=controllers,
        authz=authz,
        now=clock,
    )
    external_ref = {
        "resume_ref": {
            "transport_kind": "fake-transport",
            "transport_ref": {"handle_id": "resume-h", "session_id": "native-1"},
        },
    }
    if agent_session_id:
        external_ref["resume_ref"]["agent_session_id"] = agent_session_id
    if terminate_ref is not None:
        external_ref["terminate_ref"] = terminate_ref
    session = sessions.create_observed_session(
        session_id="observed-1",
        binding=_binding(),
        cwd="/tmp/project",
        external_ref=external_ref,
        owner=_actor("owner"),
    )
    authz.grant(session.session_id, _actor("owner"), SessionRole.OWNER)
    return orchestrator, channel, transport, controller, session


class TakeoverProcessControlTests(unittest.TestCase):
    def test_local_process_controller_terminates_authorized_process(self):
        proc = subprocess.Popen(["sleep", "60"])
        try:
            controller = LocalProcessController(timeout=2.0)
            result = asyncio.run(
                controller.terminate(
                    {"pid": proc.pid, "allow_terminate": True},
                    reason="test",
                )
            )

            self.assertTrue(result.accepted)
            proc.wait(timeout=2.0)
            self.assertIsNotNone(proc.returncode)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2.0)

    def test_terminate_sweeps_daemon_workers_of_the_same_session(self):
        # Claude Code >=2.1.2xx: the hook-recorded pid is the pty host while
        # the session lives on in a daemon worker whose cmdline carries
        # `--session-id <id>`. Terminate must kill both or headless resume is
        # refused with "currently running as a background agent".
        session_id = "cafecafe-0000-4000-8000-feedfeedfeed"
        pty_host = subprocess.Popen(["sleep", "60"])
        # A stand-in for the daemon worker: its argv carries the session id,
        # exactly like `claude --session-id <id> ...` does.
        worker = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", f"--session-id {session_id}"]
        )
        try:
            import time as _time

            _time.sleep(0.2)
            controller = LocalProcessController(timeout=2.0)
            # ADR 0053: the kill path verifies pid identity (command must match
            # the live process) via _probe_process, and the sweep keeps only
            # external-TUI commands. These stand-ins are sleep/python, so patch
            # the probe to present claude-shaped identities for them.
            import walkcode.channel_native as cn

            recorded_command = f"claude --bg-pty-host x -- claude --session-id {session_id} --resume y"
            real_probe = cn._probe_process

            def fake_probe(pid):
                # Delegate to the real probe for LIVENESS (so _wait_exited sees
                # the stand-ins actually die), only substituting the claude-
                # shaped identity while they are alive.
                real = real_probe(pid)
                if pid == pty_host.pid and real.status == "ok":
                    return cn._ProcProbe("ok", "Sun Jul 19 10:00:00 2026", recorded_command)
                if pid == worker.pid and real.status == "ok":
                    return cn._ProcProbe("ok", "Sun Jul 19 10:00:01 2026", f"claude --session-id {session_id}")
                return real

            with unittest.mock.patch.object(cn, "_probe_process", side_effect=fake_probe):
                result = asyncio.run(
                    controller.terminate(
                        {
                            "pid": pty_host.pid,
                            "allow_terminate": True,
                            "command": recorded_command,
                        },
                        reason="test",
                    )
                )

            self.assertTrue(result.accepted)
            pty_host.wait(timeout=2.0)
            worker.wait(timeout=2.0)
            self.assertIsNotNone(pty_host.returncode)
            self.assertIsNotNone(worker.returncode)
        finally:
            for proc in (pty_host, worker):
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=2.0)

    def test_local_process_controller_refuses_unauthorized_process(self):
        proc = subprocess.Popen(["sleep", "60"])
        try:
            controller = LocalProcessController(timeout=0.2)
            result = asyncio.run(controller.terminate({"pid": proc.pid}, reason="test"))

            self.assertFalse(result.accepted)
            self.assertEqual(result.reason, "termination_not_authorized")
            self.assertIsNone(proc.poll())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2.0)

    def test_takeover_resumes_before_terminating_external_tui_and_submit(self):
        order = []

        class OrderedController(FakeExternalTuiController):
            async def terminate(self, ref: dict, reason: str) -> ControlResult:
                order.append("terminate")
                return await super().terminate(ref, reason)

        class OrderedTransport(FakeAgentTransport):
            async def resume(self, spec: ResumeSpec) -> TransportHandle:
                order.append("resume")
                return await super().resume(spec)

            async def submit_turn(self, handle, turn, idempotency_key):
                order.append("submit_turn")
                await super().submit_turn(handle, turn, idempotency_key)

        orchestrator, channel, transport, controller, session = _setup(
            terminate_ref={"controller_kind": "fake-process", "process_ref": {"pid": 123}},
            controller=OrderedController("fake-process"),
            transport=OrderedTransport("fake-transport", _transport_caps()),
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run tests"),
                actor=_actor(),
                generation=session.generation,
            )
        )
        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(_action_token(channel, "takeover_and_send")),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(order, ["resume", "terminate", "submit_turn"])
        self.assertEqual(controller.terminate_calls[0]["ref"], {"pid": 123})
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["run tests"])

    def test_resume_failure_does_not_terminate_external_tui(self):
        class FailingResumeTransport(FakeAgentTransport):
            async def resume(self, spec: ResumeSpec) -> TransportHandle:
                self.resume_specs.append(spec)
                self.call_log.append("resume")
                raise RuntimeError("resume failed")

        controller = FakeExternalTuiController("fake-process")
        orchestrator, channel, transport, _controller, session = _setup(
            terminate_ref={"controller_kind": "fake-process", "process_ref": {"pid": 123}},
            controller=controller,
            transport=FailingResumeTransport("fake-transport", _transport_caps()),
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run tests"),
                actor=_actor(),
                generation=session.generation,
            )
        )
        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(_action_token(channel, "takeover_and_send")),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        updated = orchestrator.sessions.get(session.session_id)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "resume_failed")
        self.assertEqual(controller.terminate_calls, [])
        self.assertEqual(updated.writer_owner.kind, "external_tui")
        self.assertEqual(transport.call_log, ["resume"])
        self.assertEqual(transport.submitted_turns, [])

    def test_missing_terminate_ref_becomes_manual_only_even_with_resume_ref(self):
        orchestrator, channel, transport, _controller, session = _setup(
            terminate_ref=None,
            controller=FakeExternalTuiController("fake-process"),
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run tests"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(_action_token(channel, "takeover_and_send")),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        updated = orchestrator.sessions.get(session.session_id)
        tx = next(iter(orchestrator.sessions.to_dict()["takeovers"].values()))
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, TakeoverPhase.MANUAL_ONLY)
        # The clicked takeover card is now always flipped to a terminal
        # decision_result at the end; the informative view precedes it.
        self.assertEqual(channel.sent_views[-1]["view"]["type"], "decision_result")
        self.assertEqual(channel.sent_views[-2]["view"]["type"], "manual_only")
        self.assertEqual(updated.writer_owner.kind, "external_tui")
        self.assertEqual(tx["phase"], TakeoverPhase.MANUAL_ONLY)
        self.assertEqual(transport.resume_specs, [])
        self.assertEqual(transport.shutdown_calls, [])
        self.assertEqual(transport.submitted_turns, [])

    def test_process_terminate_ref_without_explicit_authorization_is_manual_only(self):
        controller = FakeExternalTuiController("process")
        orchestrator, channel, transport, _controller, session = _setup(
            terminate_ref={"controller_kind": "process", "process_ref": {"pid": 123}},
            controller=controller,
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run tests"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(_action_token(channel, "takeover_and_send")),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, TakeoverPhase.MANUAL_ONLY)
        # The clicked takeover card is now always flipped to a terminal
        # decision_result at the end; the informative view precedes it.
        self.assertEqual(channel.sent_views[-1]["view"]["type"], "decision_result")
        self.assertEqual(channel.sent_views[-2]["view"]["type"], "manual_only")
        self.assertEqual(controller.terminate_calls, [])
        self.assertEqual(transport.resume_specs, [])
        self.assertEqual(transport.shutdown_calls, [])
        self.assertEqual(transport.submitted_turns, [])

    def test_termination_failure_rolls_back_resumed_handle_without_submit_or_transfer(self):
        controller = FakeExternalTuiController("fake-process", accepted=False)
        orchestrator, channel, transport, _controller, session = _setup(
            terminate_ref={"controller_kind": "fake-process", "process_ref": {"pid": 123}},
            controller=controller,
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run tests"),
                actor=_actor(),
                generation=session.generation,
            )
        )
        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(_action_token(channel, "takeover_and_send")),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        updated = orchestrator.sessions.get(session.session_id)
        tx = next(iter(orchestrator.sessions.to_dict()["takeovers"].values()))
        blocked = next(iter(updated.blocked_inputs.values()))
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "external_tui_termination_failed")
        # The clicked takeover card is now always flipped to a terminal
        # decision_result at the end; the informative view precedes it.
        self.assertEqual(channel.sent_views[-1]["view"]["type"], "decision_result")
        self.assertEqual(channel.sent_views[-2]["view"]["type"], "takeover_progress")
        self.assertEqual(channel.sent_views[-2]["view"]["phase"], "failed")
        self.assertEqual(tx["phase"], TakeoverPhase.FAILED)
        self.assertEqual(updated.writer_owner.kind, "external_tui")
        self.assertEqual(blocked.state, "blocked")
        self.assertEqual(transport.call_log, ["resume"])
        self.assertEqual(transport.shutdown_calls, ["takeover_rollback"])
        self.assertEqual(transport.submitted_turns, [])


if __name__ == "__main__":
    unittest.main()


class TakeoverSwitchedAwayTests(unittest.TestCase):
    """ADR 0067: never stop a Claude TUI that now runs another session."""

    def _takeover(self, switched, agent_session_id="claude-old"):
        orchestrator, channel, transport, controller, session = _setup(
            terminate_ref={"controller_kind": "fake-process", "process_ref": {"pid": 123, "allow_terminate": True}},
            controller=FakeExternalTuiController("fake-process"),
            agent_session_id=agent_session_id,
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id, TurnInput(text="run tests"), actor=_actor(), generation=session.generation
            )
        )
        with unittest.mock.patch.object(Orchestrator, "_claude_tui_switched_away", side_effect=switched):
            result = asyncio.run(
                orchestrator.handle_inbound_event(
                    _callback(_action_token(channel, "takeover_and_send")),
                    agent_transport_kind="fake-transport",
                    cwd="/tmp/project",
                )
            )
        return result, controller, transport

    def test_a_terminal_that_moved_on_before_the_takeover_is_left_running(self):
        result, controller, transport = self._takeover(lambda session: True)
        self.assertTrue(result.accepted)
        self.assertEqual(controller.terminate_calls, [])
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["run tests"])

    def test_the_controller_is_told_which_session_it_may_stop(self):
        result, controller, _ = self._takeover(lambda session: False)
        self.assertTrue(result.accepted)
        self.assertEqual(controller.terminate_calls[0]["ref"]["expected_claude_session"], "claude-old")


class KillOneSwitchedSessionTests(unittest.TestCase):
    """ADR 0067: the last check sits right before each signal."""

    LSTART = "Tue Sep 29 11:06:24 2026"

    def _kill(self, sessions, *, exits_after_term=False):
        controller = LocalProcessController(kill_after_timeout=True)
        probe = channel_native_module._ProcProbe("ok", self.LSTART, "claude")
        signals = []
        with unittest.mock.patch.object(channel_native_module, "_probe_process", return_value=probe), unittest.mock.patch.object(
            channel_native_module, "claude_tui_current_session", side_effect=sessions
        ), unittest.mock.patch.object(channel_native_module.os, "kill", side_effect=lambda pid, sig: signals.append(sig)), unittest.mock.patch.object(
            controller, "_wait_exited", return_value=exits_after_term
        ):
            result = controller._kill_one(123, self.LSTART, "claude", expected_session="claude-old")
        return result, signals

    def test_no_sigterm_once_the_process_runs_another_session(self):
        result, signals = self._kill(lambda pid, lstart: "claude-new")
        self.assertEqual((result.accepted, result.state), (True, "switched_away"))
        self.assertEqual(signals, [])

    def test_no_sigkill_if_it_switched_while_we_waited(self):
        answers = iter(["claude-old", "claude-new"])
        result, signals = self._kill(lambda pid, lstart: next(answers))
        self.assertEqual(result.state, "switched_away")
        self.assertEqual(signals, [channel_native_module.signal.SIGTERM])

    def test_unknown_or_same_session_is_stopped_as_before(self):
        for answer in ("", "claude-old"):
            with self.subTest(answer=answer):
                result, signals = self._kill(lambda pid, lstart, a=answer: a, exits_after_term=True)
                self.assertEqual((result.accepted, result.state), (True, "terminated"))
                self.assertEqual(signals, [channel_native_module.signal.SIGTERM])

    def test_terminate_passes_the_expected_session_down_to_the_signal(self):
        controller = LocalProcessController(kill_after_timeout=True)
        probe = channel_native_module._ProcProbe("ok", self.LSTART, "claude")
        ref = {"pid": 123, "allow_terminate": True, "lstart": self.LSTART, "command": "claude", "expected_claude_session": "claude-old"}
        with unittest.mock.patch.object(channel_native_module, "_probe_process", return_value=probe), unittest.mock.patch.object(
            channel_native_module, "claude_tui_current_session", return_value="claude-new"
        ), unittest.mock.patch.object(channel_native_module.os, "kill", side_effect=AssertionError("signalled")):
            result = controller._terminate_sync(ref, "takeover:t1")
        self.assertEqual((result.accepted, result.state), (True, "switched_away"))

    def test_every_session_id_alias_counts(self):
        for key in ("agent_session_id", "claude_session_id", "resume", "session_id"):
            with self.subTest(key=key):
                self.assertEqual(channel_native_module.agent_session_id("claude_headless", {key: " s1 "}), "s1")
        self.assertEqual(channel_native_module.agent_session_id("claude_headless", {"session_id": 5}), "")
