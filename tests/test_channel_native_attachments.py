import asyncio
import unittest
from pathlib import Path

from walkcode.channel_native import (
    ActorRef,
    AttachmentRef,
    BlockedReason,
    ChannelBinding,
    ChannelCapabilities,
    DurableOutbox,
    FakeAgentTransport,
    FakeChannelAdapter,
    InboundEvent,
    InteractionStore,
    LarkBotApi,
    LarkChannelAdapter,
    Orchestrator,
    SessionRegistry,
    TelegramBotApi,
    TelegramChannelAdapter,
    TransportCapabilities,
)


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _actor() -> ActorRef:
    return ActorRef(channel_kind="telegram", actor_id="owner", display_name="Owner")


def _binding(kind: str = "telegram") -> ChannelBinding:
    return ChannelBinding(kind, "bot", "chat", "topic", "root")


def _channel_caps(**overrides) -> ChannelCapabilities:
    data = {
        "thread_context": True,
        "editable_message": True,
        "interactive_message": True,
        "interactive_update": True,
        "private_callback_ack": True,
        "toast_or_ephemeral_notice": True,
        "force_reply": True,
        "attachment_download": True,
        "forum_or_topic": True,
        "max_text_chars": 4096,
        "max_callback_payload_bytes": 64,
    }
    data.update(overrides)
    return ChannelCapabilities(**data)


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
        external_tui_takeover=False,
    )


class _DownloadingChannel(FakeChannelAdapter):
    def __init__(self, capabilities: ChannelCapabilities):
        super().__init__("telegram", capabilities)
        self.downloaded: list[str] = []

    async def download_attachment(self, attachment: AttachmentRef) -> AttachmentRef:
        self.downloaded.append(attachment.source_id)
        return AttachmentRef(
            source_id=attachment.source_id,
            mime=attachment.mime,
            local_path=f"/tmp/downloaded/{attachment.source_id}",
        )


class _FakeLarkDownloadApi(LarkBotApi):
    def __init__(self, content: bytes = b"file-bytes"):
        self.calls = []
        self.content = content
        super().__init__(caller=self._call)

    async def _call(self, method, payload):
        self.calls.append((method, payload))
        if method == "downloadResource":
            return {"content": self.content, "file_name": "spec.pdf"}
        return {"ok": True, "data": {"message_id": f"lark-msg-{len(self.calls)}"}}


class AttachmentIntakeTests(unittest.TestCase):
    def test_inbound_attachments_are_downloaded_before_transport_submit(self):
        clock = _Clock()
        channel = _DownloadingChannel(_channel_caps(attachment_download=True))
        transport = FakeAgentTransport("fake-transport", _transport_caps())
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=clock),
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"telegram": channel},
            transports={"fake-transport": transport},
            now=clock,
        )
        asyncio.run(orchestrator.start_session(_binding(), "fake-transport", "/tmp/project", _actor()))
        inbound = InboundEvent(
            event_id="evt-file",
            channel_kind="telegram",
            account_id="bot",
            chat_id="chat",
            thread_id="topic",
            message_id="m-file",
            root_message_id="root",
            sender_id="owner",
            sender_display="Owner",
            text="use this file",
            attachments=[AttachmentRef(source_id="file-1", mime="image/png")],
        )

        result = asyncio.run(
            orchestrator.handle_inbound_event(
                inbound,
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(channel.downloaded, ["file-1"])
        submitted = transport.submitted_turns[0]
        self.assertEqual(submitted.attachments[0].local_path, "/tmp/downloaded/file-1")
        self.assertEqual(submitted.attachments[0].mime, "image/png")

    def test_inbound_attachments_are_rejected_when_channel_cannot_download(self):
        clock = _Clock()
        channel = _DownloadingChannel(_channel_caps(attachment_download=False))
        transport = FakeAgentTransport("fake-transport", _transport_caps())
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=clock),
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"telegram": channel},
            transports={"fake-transport": transport},
            now=clock,
        )
        asyncio.run(orchestrator.start_session(_binding(), "fake-transport", "/tmp/project", _actor()))
        inbound = InboundEvent(
            event_id="evt-file",
            channel_kind="telegram",
            account_id="bot",
            chat_id="chat",
            thread_id="topic",
            message_id="m-file",
            root_message_id="root",
            sender_id="owner",
            sender_display="Owner",
            text="use this file",
            attachments=[AttachmentRef(source_id="file-1", mime="image/png")],
        )

        result = asyncio.run(
            orchestrator.handle_inbound_event(
                inbound,
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, BlockedReason.CAPABILITY_DISABLED)
        self.assertEqual(channel.downloaded, [])
        self.assertEqual(transport.submitted_turns, [])

    def test_telegram_photo_and_document_updates_create_attachment_refs(self):
        adapter = TelegramChannelAdapter(TelegramBotApi("token", caller=lambda _method, _payload: {}))

        photo = adapter.parse_update(
            {
                "update_id": 1,
                "message": {
                    "message_id": 10,
                    "chat": {"id": "chat"},
                    "from": {"id": "owner"},
                    "caption": "look",
                    "photo": [
                        {"file_id": "small", "file_size": 1},
                        {"file_id": "large", "file_size": 99},
                    ],
                },
            }
        )
        document = adapter.parse_update(
            {
                "update_id": 2,
                "message": {
                    "message_id": 11,
                    "chat": {"id": "chat"},
                    "from": {"id": "owner"},
                    "document": {
                        "file_id": "doc-1",
                        "mime_type": "application/pdf",
                        "file_name": "spec.pdf",
                    },
                },
            }
        )

        self.assertEqual(photo.attachments[0].source_id, "large")
        self.assertEqual(photo.attachments[0].mime, "image/jpeg")
        self.assertEqual(document.attachments[0].source_id, "doc-1")
        self.assertEqual(document.attachments[0].mime, "application/pdf")

    def test_lark_image_and_file_events_create_attachment_refs(self):
        adapter = LarkChannelAdapter(LarkBotApi(caller=lambda *_: {}))

        image = adapter.parse_event(
            {
                "event_id": "evt-img",
                "event": {
                    "message": {
                        "message_id": "om_img",
                        "chat_id": "oc_chat",
                        "message_type": "image",
                        "content": "{\"image_key\":\"img-1\"}",
                    },
                    "sender": {"sender_id": {"open_id": "ou_user"}},
                },
            }
        )
        file = adapter.parse_event(
            {
                "event_id": "evt-file",
                "event": {
                    "message": {
                        "message_id": "om_file",
                        "chat_id": "oc_chat",
                        "message_type": "file",
                        "content": (
                            "{\"file_key\":\"file-1\","
                            "\"file_name\":\"spec.pdf\","
                            "\"mime_type\":\"application/pdf\"}"
                        ),
                    },
                    "sender": {"sender_id": {"open_id": "ou_user"}},
                },
            }
        )

        self.assertEqual(image.attachments[0].source_id, "img-1")
        self.assertEqual(image.attachments[0].source_message_id, "om_img")
        self.assertEqual(image.attachments[0].mime, "image/*")
        self.assertEqual(file.attachments[0].source_id, "file-1")
        self.assertEqual(file.attachments[0].source_message_id, "om_file")
        self.assertEqual(file.attachments[0].mime, "application/pdf")

    def test_lark_download_attachment_writes_local_file(self):
        api = _FakeLarkDownloadApi(content=b"%PDF-1.7")
        adapter = LarkChannelAdapter(api)

        downloaded = asyncio.run(
            adapter.download_attachment(
                AttachmentRef(
                    source_id="file-1",
                    mime="application/pdf",
                    source_message_id="om_file",
                )
            )
        )

        self.assertEqual(api.calls[0][0], "downloadResource")
        self.assertEqual(api.calls[0][1]["message_id"], "om_file")
        self.assertEqual(api.calls[0][1]["file_key"], "file-1")
        self.assertEqual(api.calls[0][1]["type"], "file")
        self.assertEqual(Path(downloaded.local_path).read_bytes(), b"%PDF-1.7")
        self.assertEqual(downloaded.source_message_id, "om_file")

    def test_lark_inbound_attachment_is_downloaded_before_transport_submit(self):
        clock = _Clock()
        api = _FakeLarkDownloadApi(content=b"image-bytes")
        channel = LarkChannelAdapter(api)
        transport = FakeAgentTransport("fake-transport", _transport_caps())
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=clock),
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"lark": channel},
            transports={"fake-transport": transport},
            now=clock,
        )
        event = channel.parse_event(
            {
                "event_id": "evt-img",
                "event": {
                    "message": {
                        "message_id": "om_img",
                        "chat_id": "oc_chat",
                        "message_type": "image",
                        "content": "{\"image_key\":\"img-1\"}",
                    },
                    "sender": {"sender_id": {"open_id": "ou_user"}},
                },
            }
        )

        result = asyncio.run(
            orchestrator.handle_inbound_event(
                event,
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertTrue(result.accepted)
        submitted = transport.submitted_turns[0]
        self.assertEqual(Path(submitted.attachments[0].local_path).read_bytes(), b"image-bytes")
        self.assertEqual(submitted.attachments[0].source_message_id, "om_img")


if __name__ == "__main__":
    unittest.main()
