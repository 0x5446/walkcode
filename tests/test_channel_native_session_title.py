"""Session-title refresh across the four turn-end paths.

The thread-root card shows ``session.cached_title``. Four different signals can
end a turn and each one used to be a place where a title rule could drift:

  claude TUI / codex TUI      -> hooks.json Stop + UserPromptSubmit
  codex app-server            -> event stream TURN_COMPLETED
  claude headless             -> event stream TURN_COMPLETED

codex is the reason the split exists: ``codex app-server`` never loads the
user-level hooks.json (it only emits hook events for source="plugin"), so a
Stop hook is not a fallback there — the event stream is the only signal.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path

from walkcode.channel_native import (
    ROOT_CARD_EDIT_RETRY_BUDGET,
    SESSION_TITLE_MATERIAL_CHARS,
    SESSION_TITLE_REFRESH_INTERVAL_SECONDS,
    ActorRef,
    AgentEvent,
    AgentEventType,
    ChannelBinding,
    ChannelCapabilities,
    ChannelNativeConfig,
    CodexAppServerTransport,
    DurableOutbox,
    FakeAgentTransport,
    FakeChannelAdapter,
    InteractionStore,
    JsonFileStateStore,
    Orchestrator,
    PermanentDeliveryError,
    SessionRegistry,
    TelegramBotApi,
    TransportCapabilities,
    TurnInput,
    compose_session_title,
)
from walkcode.channel_native_runtime import ChannelNativeRuntime


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _transport_caps() -> TransportCapabilities:
    return TransportCapabilities(
        structured_input=True,
        structured_output=True,
        permission_callback=True,
        ask_user_question=True,
        interrupt=True,
        set_model=True,
        set_permission_mode=True,
        checkpoint_rewind=True,
        resume_after_complete=True,
        resume_active_turn=False,
        multi_client_observe=False,
        multi_client_write=False,
        external_tui_takeover=True,
    )


def _channel_caps() -> ChannelCapabilities:
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


def _binding() -> ChannelBinding:
    binding = ChannelBinding(
        channel_kind="fake",
        account_id="bot",
        chat_id="chat-1",
        thread_id="topic-1",
        root_message_id="root-1",
    )
    binding.capabilities["status_card"] = True
    return binding


def _actor() -> ActorRef:
    return ActorRef(channel_kind="fake", actor_id="user-1", display_name="User One")


class _TitleTelegramApi(TelegramBotApi):
    """Minimal stub covering only the calls a TUI hook turn makes."""

    def __init__(self):
        self.calls = []
        super().__init__(token="fake", caller=self._call)

    async def _call(self, method, payload):
        self.calls.append((method, dict(payload)))
        if method == "getMe":
            return {
                "ok": True,
                "result": {
                    "id": 123456,
                    "username": "walkcode_title_bot",
                    "first_name": "WalkCode",
                    "can_join_groups": True,
                    "can_read_all_group_messages": False,
                    "has_topics_enabled": False,
                    "allows_users_to_create_topics": False,
                },
            }
        if method == "getChat":
            return {"ok": True, "result": {"id": payload.get("chat_id"), "type": "private", "is_forum": False}}
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": len(self.calls)}}
        if method in {
            "editMessageText",
            "sendChatAction",
            "setMessageReaction",
            "setMyCommands",
            "pinChatMessage",
            "deleteMessage",
        }:
            return {"ok": True, "result": True}
        raise AssertionError(f"unexpected Telegram method: {method}")


def _tui_runtime(tmp: str, agent: str, api: _TitleTelegramApi) -> ChannelNativeRuntime:
    cfg = ChannelNativeConfig.from_env(
        {
            "WALKCODE_CHANNEL": "telegram",
            "TELEGRAM_BOT_TOKEN": "fake",
            "WALKCODE_AGENT": agent,
            "TELEGRAM_ALLOWED_CHAT_IDS": "123",
            "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
            "WALKCODE_CWD": tmp,
        }
    )
    transport_kind = "codex_app_server" if agent == "codex" else "claude_headless"
    return ChannelNativeRuntime.from_config(
        cfg,
        telegram_api=api,
        transports={transport_kind: FakeAgentTransport(transport_kind, _transport_caps())},
    )


def _only_session(runtime: ChannelNativeRuntime):
    summaries = runtime.state.sessions.list_sessions(channel_kind="telegram")
    assert len(summaries) == 1, summaries
    return runtime.state.sessions.get(summaries[0].session_id)


def _structured_fixture(clock: _Clock, transport_kind: str, message: str):
    channel = FakeChannelAdapter("fake", _channel_caps())
    transport = FakeAgentTransport(
        transport_kind,
        _transport_caps(),
        scripted_events=[AgentEvent(AgentEventType.TURN_COMPLETED, {"message": message})],
    )
    orchestrator = Orchestrator(
        sessions=SessionRegistry(now=clock),
        interactions=InteractionStore(now=clock),
        outbox=DurableOutbox(now=clock),
        channels={"fake": channel},
        transports={transport_kind: transport},
        now=clock,
    )
    return orchestrator, channel


class _RealProtocolCodexClient:
    """Emits the JSON-RPC shape codex app-server actually sends on the wire.

    The distinction that matters here: `turn/completed` carries only threadId
    and turn — never the assistant text. That text arrived earlier as separate
    `item/agentMessage/delta` notifications. A test that fabricates a
    TURN_COMPLETED with a `message` field cannot catch a title path that only
    reads the completion payload.
    """

    def __init__(self, events):
        self.event_batches = {"thread-1": list(events)}
        self.requests = []

    async def request(self, method, params):
        self.requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        return {}

    async def events(self, thread_id):
        return self.event_batches.pop(thread_id, [])

    async def answer_request(self, request_id, result):
        return None


def _codex_delta(text: str) -> dict:
    return {
        "method": "item/agentMessage/delta",
        "params": {"threadId": "thread-1", "turnId": "turn-1", "delta": text},
    }


_CODEX_TURN_COMPLETED = {
    "method": "turn/completed",
    "params": {"threadId": "thread-1", "turn": {"id": "turn-1"}},
}


class ComposeSessionTitleTests(unittest.TestCase):
    def test_user_text_wins_over_assistant_text(self):
        self.assertEqual(
            compose_session_title(user_text="改话题根标题", assistant_text="已经改好了"),
            ("改话题根标题", "initial_user_input"),
        )

    def test_assistant_text_is_the_fallback(self):
        self.assertEqual(
            compose_session_title(user_text="   ", assistant_text="已经改好了"),
            ("已经改好了", "turn_digest"),
        )

    def test_first_non_blank_line_collapsed_and_clipped(self):
        title, source = compose_session_title(user_text="\n\n  给   话题根   一个标题  \n第二行")
        self.assertEqual(title, "给 话题根 一个标题")
        self.assertEqual(source, "initial_user_input")
        long_title, _ = compose_session_title(user_text="标" * 90)
        self.assertEqual(len(long_title), 40)

    def test_no_material_yields_no_title(self):
        self.assertEqual(compose_session_title(user_text="", assistant_text=""), ("", ""))


class TuiHookTitlePathTests(unittest.TestCase):
    """claude TUI and codex TUI — both deliver turn end through hooks.json."""

    def test_claude_tui_prompt_replaces_the_uuid_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = _TitleTelegramApi()
            runtime = _tui_runtime(tmp, "claude", api)

            asyncio.run(
                runtime.process_tui_hook(
                    hook_type="SessionStart",
                    agent="claude",
                    payload={"session_id": "claude-title-1", "cwd": tmp},
                )
            )
            placeholder = _only_session(runtime)
            self.assertEqual(placeholder.title_source, "tui_hook")
            self.assertIn("claude-title-1", placeholder.cached_title)

            asyncio.run(
                runtime.process_tui_hook(
                    hook_type="UserPromptSubmit",
                    agent="claude",
                    payload={
                        "session_id": "claude-title-1",
                        "cwd": tmp,
                        "prompt": "把话题根标题改成有意义的",
                    },
                )
            )

            session = _only_session(runtime)
            self.assertEqual(session.cached_title, "把话题根标题改成有意义的")
            self.assertEqual(session.title_source, "initial_user_input")

    def test_codex_tui_stop_hook_digests_when_the_prompt_was_missed(self):
        # Attaching mid-flight: the first prompt was submitted before the hook
        # pipeline was watching, so the stop bubble is the only material.
        with tempfile.TemporaryDirectory() as tmp:
            api = _TitleTelegramApi()
            runtime = _tui_runtime(tmp, "codex", api)

            asyncio.run(
                runtime.process_tui_hook(
                    hook_type="SessionStart",
                    agent="codex",
                    payload={"session_id": "codex-title-1", "cwd": tmp},
                )
            )
            asyncio.run(
                runtime.process_tui_hook(
                    hook_type="Stop",
                    agent="codex",
                    payload={
                        "session_id": "codex-title-1",
                        "cwd": tmp,
                        "last_assistant_message": "常驻事件监听已经修好了",
                    },
                )
            )

            session = _only_session(runtime)
            self.assertEqual(session.cached_title, "常驻事件监听已经修好了")
            self.assertEqual(session.title_source, "turn_digest")

    def test_later_prompt_does_not_repaint_the_first_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = _TitleTelegramApi()
            runtime = _tui_runtime(tmp, "claude", api)

            for prompt in ("第一个问题", "换个话题问第二个"):
                asyncio.run(
                    runtime.process_tui_hook(
                        hook_type="UserPromptSubmit",
                        agent="claude",
                        payload={"session_id": "claude-title-2", "cwd": tmp, "prompt": prompt},
                    )
                )

            session = _only_session(runtime)
            self.assertEqual(session.cached_title, "第一个问题")
            self.assertEqual(session.title_source, "initial_user_input")


class EventStreamTitlePathTests(unittest.TestCase):
    """codex app-server and claude headless — no hooks, only TURN_COMPLETED."""

    def _run_one_turn(self, transport_kind: str, message: str, clock: _Clock):
        orchestrator, channel = _structured_fixture(clock, transport_kind, message)
        session = asyncio.run(
            orchestrator.start_session(
                binding=_binding(),
                transport_kind=transport_kind,
                cwd="/tmp/project",
                owner=_actor(),
            )
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="跑一轮"),
                actor=_actor(),
                generation=session.generation,
            )
        )
        return session, channel

    def test_codex_app_server_turn_completed_sets_the_title(self):
        session, channel = self._run_one_turn(
            "codex_app_server", "常驻事件监听已经修好了", _Clock()
        )

        self.assertEqual(session.cached_title, "常驻事件监听已经修好了")
        self.assertEqual(session.title_source, "turn_digest")
        health_titles = [
            item["view"].get("title")
            for item in channel.sent_views
            if item["view"].get("type") == "health"
        ]
        self.assertIn("常驻事件监听已经修好了", health_titles)

    def test_claude_headless_turn_completed_sets_the_title(self):
        session, _ = self._run_one_turn("claude_headless", "补建话题根跑通了", _Clock())

        self.assertEqual(session.cached_title, "补建话题根跑通了")
        self.assertEqual(session.title_source, "turn_digest")

    def test_real_codex_protocol_completion_without_body_still_titles(self):
        # Regression for the bug the fabricated-event tests above cannot see:
        # on the real wire the completion notification has no assistant text,
        # so the title has to come from the deltas that preceded it.
        clock = _Clock()
        channel = FakeChannelAdapter("fake", _channel_caps())
        client = _RealProtocolCodexClient(
            [
                _codex_delta("常驻事件监听"),
                _codex_delta("已经修好了"),
                _CODEX_TURN_COMPLETED,
            ]
        )
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=clock),
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"fake": channel},
            transports={"codex_app_server": transport},
            now=clock,
        )
        session = asyncio.run(
            orchestrator.start_session(
                binding=_binding(),
                transport_kind="codex_app_server",
                cwd="/tmp/project",
                owner=_actor(),
            )
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="跑一轮"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        self.assertEqual(session.cached_title, "常驻事件监听已经修好了")
        self.assertEqual(session.title_source, "turn_digest")

    def test_failed_turn_text_does_not_leak_into_the_next_turn(self):
        # SESSION_ERROR closes a turn too. If the accumulator only reset on
        # completion, a failed turn's text would title the NEXT turn on this
        # long-lived stream.
        clock = _Clock()
        orchestrator, _ = _structured_fixture(clock, "codex_app_server", "unused")
        orchestrator.transports["codex_app_server"] = FakeAgentTransport(
            "codex_app_server",
            _transport_caps(),
            scripted_events=[
                AgentEvent(AgentEventType.TURN_DELTA, {"text": "这一轮失败了的正文"}),
                AgentEvent(AgentEventType.SESSION_ERROR, {"message": "boom"}),
                AgentEvent(AgentEventType.TURN_COMPLETED, {"message": ""}),
            ],
        )
        session = asyncio.run(
            orchestrator.start_session(
                binding=_binding(),
                transport_kind="codex_app_server",
                cwd="/tmp/project",
                owner=_actor(),
            )
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="跑一轮"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        self.assertEqual(session.cached_title, "")
        self.assertEqual(session.title_source, "")

    def test_whitespace_completion_falls_back_to_accumulated_text(self):
        # A whitespace message is truthy; a bare `or` would let it shadow the
        # deltas and clean down to an empty title.
        clock = _Clock()
        orchestrator, _ = _structured_fixture(clock, "codex_app_server", "unused")
        orchestrator.transports["codex_app_server"] = FakeAgentTransport(
            "codex_app_server",
            _transport_caps(),
            scripted_events=[
                AgentEvent(AgentEventType.TURN_DELTA, {"text": "真正的正文在这里"}),
                AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "   \n  "}),
            ],
        )
        session = asyncio.run(
            orchestrator.start_session(
                binding=_binding(),
                transport_kind="codex_app_server",
                cwd="/tmp/project",
                owner=_actor(),
            )
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="跑一轮"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        self.assertEqual(session.cached_title, "真正的正文在这里")

    def test_one_huge_delta_is_clipped_to_the_material_budget(self):
        # The transport coalesces a whole batch of deltas into ONE event, so
        # checking the length before appending the whole string would blow the
        # cap by orders of magnitude.
        clock = _Clock()
        orchestrator, _ = _structured_fixture(clock, "codex_app_server", "unused")
        captured: list[str] = []
        original = orchestrator._maybe_refresh_session_title

        async def _capture(session, *, user_text="", assistant_text=""):
            captured.append(assistant_text)
            return await original(session, user_text=user_text, assistant_text=assistant_text)

        orchestrator._maybe_refresh_session_title = _capture
        orchestrator.transports["codex_app_server"] = FakeAgentTransport(
            "codex_app_server",
            _transport_caps(),
            scripted_events=[
                AgentEvent(AgentEventType.TURN_DELTA, {"text": "标" * 50_000}),
                AgentEvent(AgentEventType.TURN_COMPLETED, {"message": ""}),
            ],
        )
        session = asyncio.run(
            orchestrator.start_session(
                binding=_binding(),
                transport_kind="codex_app_server",
                cwd="/tmp/project",
                owner=_actor(),
            )
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="跑一轮"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        self.assertTrue(captured)
        self.assertLessEqual(len(captured[-1]), SESSION_TITLE_MATERIAL_CHARS)
        self.assertEqual(session.cached_title, "标" * 40)

    def test_channel_initiated_title_outranks_the_turn_digest(self):
        clock = _Clock()
        orchestrator, _ = _structured_fixture(clock, "codex_app_server", "助手说了别的")
        binding = _binding()
        binding.capabilities["initial_title"] = "用户在飞书里问的原话"
        session = asyncio.run(
            orchestrator.start_session(
                binding=binding,
                transport_kind="codex_app_server",
                cwd="/tmp/project",
                owner=_actor(),
            )
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="跑一轮"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        self.assertEqual(session.cached_title, "用户在飞书里问的原话")
        self.assertEqual(session.title_source, "initial_user_input")


class TitleRankAndThrottleTests(unittest.TestCase):
    def _orchestrator_and_session(self, clock: _Clock):
        orchestrator, _ = _structured_fixture(clock, "codex_app_server", "unused")
        session = asyncio.run(
            orchestrator.start_session(
                binding=_binding(),
                transport_kind="codex_app_server",
                cwd="/tmp/project",
                owner=_actor(),
            )
        )
        return orchestrator, session

    def test_same_rank_digest_refresh_is_throttled(self):
        clock = _Clock()
        orchestrator, session = self._orchestrator_and_session(clock)

        self.assertTrue(
            asyncio.run(
                orchestrator._maybe_refresh_session_title(session, assistant_text="第一段结论")
            )
        )
        clock.now += SESSION_TITLE_REFRESH_INTERVAL_SECONDS - 1
        self.assertFalse(
            asyncio.run(
                orchestrator._maybe_refresh_session_title(session, assistant_text="第二段结论")
            )
        )
        self.assertEqual(session.cached_title, "第一段结论")

        clock.now += 2
        self.assertTrue(
            asyncio.run(
                orchestrator._maybe_refresh_session_title(session, assistant_text="第三段结论")
            )
        )
        self.assertEqual(session.cached_title, "第三段结论")

    def test_rank_upgrade_ignores_the_throttle(self):
        clock = _Clock()
        orchestrator, session = self._orchestrator_and_session(clock)

        asyncio.run(orchestrator._maybe_refresh_session_title(session, assistant_text="助手结论"))
        # Same instant: a stronger source must not have to wait out the window.
        self.assertTrue(
            asyncio.run(orchestrator._maybe_refresh_session_title(session, user_text="用户原话"))
        )
        self.assertEqual(session.cached_title, "用户原话")
        self.assertEqual(session.title_source, "initial_user_input")

    def test_weaker_source_never_repaints_a_stronger_title(self):
        # A codex thread that hops TUI <-> app-server keeps flipping which
        # signal arrives; the digest must not undo the prompt title on a hop.
        clock = _Clock()
        orchestrator, session = self._orchestrator_and_session(clock)

        asyncio.run(orchestrator._maybe_refresh_session_title(session, user_text="用户原话"))
        clock.now += SESSION_TITLE_REFRESH_INTERVAL_SECONDS * 10
        self.assertFalse(
            asyncio.run(orchestrator._maybe_refresh_session_title(session, assistant_text="助手结论"))
        )
        self.assertEqual(session.cached_title, "用户原话")
        self.assertEqual(session.title_source, "initial_user_input")

    def test_identical_digest_does_not_move_the_watermark(self):
        clock = _Clock()
        orchestrator, session = self._orchestrator_and_session(clock)

        asyncio.run(orchestrator._maybe_refresh_session_title(session, assistant_text="同一句"))
        first_stamp = session.title_refreshed_at
        clock.now += SESSION_TITLE_REFRESH_INTERVAL_SECONDS * 2
        self.assertFalse(
            asyncio.run(orchestrator._maybe_refresh_session_title(session, assistant_text="同一句"))
        )
        self.assertEqual(session.title_refreshed_at, first_stamp)


class StatusCardFingerprintTests(unittest.TestCase):
    """Swapping the status card must not be swallowed by the dedup cache."""

    def _orchestrator_and_session(self, clock: _Clock):
        orchestrator, channel = _structured_fixture(clock, "codex_app_server", "unused")
        session = asyncio.run(
            orchestrator.start_session(
                binding=_binding(),
                transport_kind="codex_app_server",
                cwd="/tmp/project",
                owner=_actor(),
            )
        )
        return orchestrator, session, channel

    def test_unchanged_view_is_still_deduped(self):
        clock = _Clock()
        orchestrator, session, channel = self._orchestrator_and_session(clock)

        asyncio.run(orchestrator.refresh_session_status_card(session))
        before = len(channel.sent_views)
        asyncio.run(orchestrator.refresh_session_status_card(session))

        self.assertEqual(len(channel.sent_views), before)

    def test_pointing_at_a_new_card_invalidates_the_fingerprint(self):
        # The rootless-Lark heal moves the status card onto a freshly sent
        # root. Nothing clears the cache for it — the (message_id, fingerprint)
        # key has to make the stale entry stop matching on its own, or the new
        # card freezes at whatever state it was created with.
        clock = _Clock()
        orchestrator, session, channel = self._orchestrator_and_session(clock)

        asyncio.run(orchestrator.refresh_session_status_card(session))
        session.channel_binding.health_message_id = "healed-root-card"
        before = len(channel.sent_views)
        asyncio.run(orchestrator.refresh_session_status_card(session))

        self.assertEqual(len(channel.sent_views), before + 1)
        self.assertEqual(channel.sent_views[-1]["message_id"], "healed-root-card")
        self.assertTrue(channel.sent_views[-1]["edited"])

        # Second half of the contract: the fingerprint written by the edit
        # branch must itself dedup. Without this the edit branch could store a
        # stale-format key and every later event would re-patch the same card.
        asyncio.run(orchestrator.refresh_session_status_card(session))
        self.assertEqual(len(channel.sent_views), before + 1)

    def test_fingerprint_after_send_fallback_also_dedups(self):
        # The send path (edit failed on a non-root card) records its own
        # fingerprint; an unchanged view afterwards must not re-send.
        clock = _Clock()
        channel = _EditFailingChannel("fake", _channel_caps(), fail_edits=1)
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=clock),
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"fake": channel},
            transports={
                "codex_app_server": FakeAgentTransport(
                    "codex_app_server", _transport_caps(), scripted_events=[]
                )
            },
            now=clock,
        )
        session = asyncio.run(
            orchestrator.start_session(
                binding=_binding(),
                transport_kind="codex_app_server",
                cwd="/tmp/project",
                owner=_actor(),
            )
        )
        session.channel_binding.health_message_id = "child-card"

        asyncio.run(orchestrator.refresh_session_status_card(session))
        sends_after_fallback = channel.sends
        edits_after_fallback = channel.edit_attempts
        asyncio.run(orchestrator.refresh_session_status_card(session))

        self.assertEqual(channel.sends, sends_after_fallback)
        self.assertEqual(channel.edit_attempts, edits_after_fallback)


class _EditFailingChannel(FakeChannelAdapter):
    """Fails the first N edits, then behaves normally."""

    def __init__(self, kind, capabilities, fail_edits=1, raise_instead=False):
        super().__init__(kind, capabilities)
        self.fail_edits = fail_edits
        self.raise_instead = raise_instead
        self.sends = 0
        self.edit_attempts = 0

    async def send_view(self, binding, view_model):
        self.sends += 1
        return await super().send_view(binding, view_model)

    async def edit_view(self, binding, message_id, view_model):
        self.edit_attempts += 1
        if self.fail_edits > 0:
            self.fail_edits -= 1
            if self.raise_instead:
                raise RuntimeError("lark edit blew up")
            return False
        return await super().edit_view(binding, message_id, view_model)


class _PermanentEditFailureChannel(FakeChannelAdapter):
    """Every edit fails the way Lark reports a hopeless one."""

    def __init__(self, kind, capabilities):
        super().__init__(kind, capabilities)
        self.sends = 0
        self.edit_attempts = 0

    async def send_view(self, binding, view_model):
        self.sends += 1
        return await super().send_view(binding, view_model)

    async def edit_view(self, binding, message_id, view_model):
        self.edit_attempts += 1
        raise PermanentDeliveryError("message not found")


class RootCardEditFailureTests(unittest.TestCase):
    """A failed edit must not move the status card off the thread root."""

    def _session_on(self, channel, clock, *, root_is_status_card):
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=clock),
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"fake": channel},
            transports={
                "codex_app_server": FakeAgentTransport(
                    "codex_app_server", _transport_caps(), scripted_events=[]
                )
            },
            now=clock,
        )
        binding = _binding()
        session = asyncio.run(
            orchestrator.start_session(
                binding=binding,
                transport_kind="codex_app_server",
                cwd="/tmp/project",
                owner=_actor(),
            )
        )
        # Mirror the Lark TUI observed binding: the thread root doubles as the
        # status card. The contrasting case pins the card to a child message.
        session.channel_binding.health_message_id = (
            session.channel_binding.root_message_id if root_is_status_card else "child-card"
        )
        return orchestrator, session

    def test_failed_root_edit_keeps_the_pointer_and_sends_nothing(self):
        # Lark cannot replace a thread root, so a replacement card would land
        # as a reply UNDER the root and strand the root on its old title.
        clock = _Clock()
        channel = _EditFailingChannel("fake", _channel_caps(), fail_edits=1)
        orchestrator, session = self._session_on(channel, clock, root_is_status_card=True)
        root_id = session.channel_binding.root_message_id

        asyncio.run(orchestrator.refresh_session_status_card(session))

        self.assertEqual(channel.edit_attempts, 1)
        self.assertEqual(channel.sends, 0)
        self.assertEqual(session.channel_binding.health_message_id, root_id)

    def test_root_edit_retries_on_the_next_refresh(self):
        # The failed attempt must not be recorded as a fingerprint, or the
        # retry would be deduped away and the root frozen forever.
        clock = _Clock()
        channel = _EditFailingChannel("fake", _channel_caps(), fail_edits=1)
        orchestrator, session = self._session_on(channel, clock, root_is_status_card=True)
        root_id = session.channel_binding.root_message_id

        asyncio.run(orchestrator.refresh_session_status_card(session))
        asyncio.run(orchestrator.refresh_session_status_card(session))

        self.assertEqual(channel.edit_attempts, 2)
        self.assertEqual(channel.sends, 0)
        self.assertEqual(session.channel_binding.health_message_id, root_id)
        self.assertTrue(channel.sent_views[-1].get("edited"))
        self.assertEqual(channel.sent_views[-1]["message_id"], root_id)

    def test_raising_root_edit_is_handled_like_a_false_return(self):
        clock = _Clock()
        channel = _EditFailingChannel("fake", _channel_caps(), fail_edits=1, raise_instead=True)
        orchestrator, session = self._session_on(channel, clock, root_is_status_card=True)
        root_id = session.channel_binding.root_message_id

        asyncio.run(orchestrator.refresh_session_status_card(session))

        self.assertEqual(channel.sends, 0)
        self.assertEqual(session.channel_binding.health_message_id, root_id)

    def test_permanently_broken_root_demotes_to_a_child_card(self):
        # A deleted / recalled / past-edit-window root fails identically every
        # time. Retrying forever burns quota and leaves the user with a status
        # card that never updates again — give up on the root instead.
        clock = _Clock()
        channel = _EditFailingChannel("fake", _channel_caps(), fail_edits=99)
        orchestrator, session = self._session_on(channel, clock, root_is_status_card=True)
        root_id = session.channel_binding.root_message_id

        for _ in range(ROOT_CARD_EDIT_RETRY_BUDGET + 2):
            asyncio.run(orchestrator.refresh_session_status_card(session))

        # Budget spent → exactly one demotion send, then the child card takes
        # over; the root is no longer hammered on every event.
        self.assertEqual(channel.sends, 1)
        self.assertEqual(channel.edit_attempts, ROOT_CARD_EDIT_RETRY_BUDGET)
        self.assertNotEqual(session.channel_binding.health_message_id, root_id)
        self.assertNotEqual(session.channel_binding.health_message_id, "")

    def test_permanent_delivery_error_demotes_without_spending_the_budget(self):
        # A permanent error is known-hopeless on attempt one; waiting out the
        # retry budget would just be three guaranteed failures.
        clock = _Clock()
        channel = _PermanentEditFailureChannel("fake", _channel_caps())
        orchestrator, session = self._session_on(channel, clock, root_is_status_card=True)

        asyncio.run(orchestrator.refresh_session_status_card(session))

        self.assertEqual(channel.edit_attempts, 1)
        self.assertEqual(channel.sends, 1)

    def test_a_successful_edit_clears_the_failure_budget(self):
        # The budget exists to escape a broken root, not to tally lifetime
        # blips on a healthy one.
        clock = _Clock()
        channel = _EditFailingChannel("fake", _channel_caps(), fail_edits=1)
        orchestrator, session = self._session_on(channel, clock, root_is_status_card=True)

        asyncio.run(orchestrator.refresh_session_status_card(session))
        self.assertEqual(session.channel_binding.capabilities.get("root_card_edit_failures"), 1)
        asyncio.run(orchestrator.refresh_session_status_card(session))

        self.assertNotIn("root_card_edit_failures", session.channel_binding.capabilities)
        self.assertEqual(channel.sends, 0)

    def test_non_root_status_card_still_falls_back_to_a_new_card(self):
        # The guard must be narrow: a status card that is NOT the root has no
        # reason to lose the existing recovery path.
        clock = _Clock()
        channel = _EditFailingChannel("fake", _channel_caps(), fail_edits=1)
        orchestrator, session = self._session_on(channel, clock, root_is_status_card=False)

        asyncio.run(orchestrator.refresh_session_status_card(session))

        self.assertEqual(channel.sends, 1)
        self.assertNotEqual(session.channel_binding.health_message_id, "child-card")
        self.assertNotEqual(session.channel_binding.health_message_id, "")


class TitlePersistenceTests(unittest.TestCase):
    def test_title_watermark_survives_a_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = _TitleTelegramApi()
            runtime = _tui_runtime(tmp, "claude", api)
            asyncio.run(
                runtime.process_tui_hook(
                    hook_type="UserPromptSubmit",
                    agent="claude",
                    payload={"session_id": "claude-title-3", "cwd": tmp, "prompt": "保存后还在吗"},
                )
            )
            live = _only_session(runtime)

            snapshot = JsonFileStateStore(str(Path(tmp) / "state.json")).load()
            restored = snapshot.sessions.get(live.session_id)

            self.assertEqual(restored.cached_title, "保存后还在吗")
            self.assertEqual(restored.title_source, "initial_user_input")
            self.assertEqual(restored.title_refreshed_at, live.title_refreshed_at)
            self.assertGreater(restored.title_refreshed_at, 0.0)


if __name__ == "__main__":
    unittest.main()
