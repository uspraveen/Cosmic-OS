from __future__ import annotations

import json

import httpx
import pytest

from gateway.channels.telegram import TelegramAdapter, TelegramConfig


def build_adapter(
    sent_calls: list[tuple[str, dict[str, object]]],
    *,
    auto_configure_webhook: bool = False,
    webhook_info_url: str = "https://user.thelearnchain.com/channels/telegram/webhook",
) -> TelegramAdapter:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8")) if request.content else {}
        sent_calls.append((request.url.path, payload))

        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 1001, "username": "cosmic_test_bot"}})
        if request.url.path.endswith("/getWebhookInfo"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "url": webhook_info_url,
                        "has_custom_certificate": False,
                        "pending_update_count": 0,
                    },
                },
            )
        if request.url.path.endswith("/setWebhook"):
            return httpx.Response(200, json={"ok": True, "result": True})
        if request.url.path.endswith("/deleteWebhook"):
            return httpx.Response(200, json={"ok": True, "result": True})
        if request.url.path.endswith("/sendChatAction"):
            return httpx.Response(200, json={"ok": True, "result": True})
        if request.url.path.endswith("/sendMessage"):
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200,
                json={"ok": True, "result": {"file_id": payload["file_id"], "file_path": "photos/image.jpg"}},
            )
        if request.url.path.endswith("/file/botbot-token/photos/image.jpg"):
            return httpx.Response(200, content=b"image-bytes", headers={"content-type": "image/jpeg"})
        return httpx.Response(404, json={"ok": False, "description": "not found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = TelegramConfig(
        enabled=True,
        bot_token="bot-token",
        webhook_secret="telegram-secret",
        public_base_url="https://user.thelearnchain.com",
        auto_configure_webhook=auto_configure_webhook,
        allowed_user_id=12345,
        allowed_chat_id=12345,
    )
    return TelegramAdapter(config, http_client=client)


@pytest.mark.asyncio
async def test_telegram_adapter_normalizes_private_image_message_to_artifact_manifest() -> None:
    adapter = build_adapter([])
    normalized = adapter.normalize_message(
        {
            "update_id": 123,
            "message": {
                "message_id": 5,
                "date": 1773100000,
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 12345, "username": "praveen", "first_name": "Praveen"},
                "caption": "check this",
                "photo": [
                    {"file_id": "small", "file_unique_id": "uniq_small", "width": 90, "height": 90, "file_size": 900},
                    {"file_id": "large", "file_unique_id": "uniq_large", "width": 1280, "height": 720, "file_size": 204800},
                ],
            },
        }
    )

    assert normalized is not None
    assert normalized["content"] == "check this"
    assert normalized["channel"] == "telegram:chat_12345"
    attachments = normalized["metadata"]["attachments"]
    assert len(attachments) == 1
    attachment = attachments[0]
    assert attachment["kind"] == "image"
    assert attachment["bridge_media_ref"] == "telegram:file:large"
    assert attachment["download_url"] == "/internal/channels/telegram/media/large"
    assert attachment["width"] == 1280
    assert attachment["height"] == 720


@pytest.mark.asyncio
async def test_telegram_adapter_ignores_non_private_or_non_allowed_users() -> None:
    adapter = build_adapter([])

    assert (
        adapter.normalize_message(
            {
                "message": {
                    "message_id": 7,
                    "chat": {"id": -100, "type": "group"},
                    "from": {"id": 12345},
                    "text": "hi",
                }
            }
        )
        is None
    )
    assert (
        adapter.normalize_message(
            {
                "message": {
                    "message_id": 8,
                    "chat": {"id": 999, "type": "private"},
                    "from": {"id": 999},
                    "text": "hi",
                }
            }
        )
        is None
    )


@pytest.mark.asyncio
async def test_telegram_adapter_buffers_chunks_and_sends_typing_then_final_text() -> None:
    sent_calls: list[tuple[str, dict[str, object]]] = []
    adapter = build_adapter(sent_calls)
    try:
        await adapter.start()
        sent_calls.clear()

        await adapter.send(
            {"type": "route_result", "request_id": "req_tg_1", "channel": "telegram:chat_12345"},
            channel="telegram:chat_12345",
        )
        await adapter.send(
            {
                "type": "response.chunk",
                "request_id": "req_tg_1",
                "content": "## Summary\n\n**Hello** [Site](https://example.com)\n\n| Model | Speed |\n| --- | --- |\n| Haiku | Fast |",
                "channel": "telegram:chat_12345",
            },
            channel="telegram:chat_12345",
        )
        await adapter.send(
            {
                "type": "response.complete",
                "request_id": "req_tg_1",
                "content": "",
                "channel": "telegram:chat_12345",
            },
            channel="telegram:chat_12345",
        )
    finally:
        await adapter.stop()

    assert sent_calls[0] == (
        "/botbot-token/sendChatAction",
        {"chat_id": 12345, "action": "typing"},
    )
    assert sent_calls[1] == (
        "/botbot-token/sendMessage",
        {
            "chat_id": 12345,
            "text": "Summary\n\nHello Site: https://example.com\n\n• Model: Haiku | Speed: Fast",
            "disable_web_page_preview": True,
        },
    )


@pytest.mark.asyncio
async def test_telegram_adapter_syncs_webhook_and_downloads_media() -> None:
    sent_calls: list[tuple[str, dict[str, object]]] = []
    adapter = build_adapter(sent_calls, auto_configure_webhook=True)
    try:
        await adapter.start()
        set_webhook_call = next(call for call in sent_calls if call[0] == "/botbot-token/setWebhook")
        assert set_webhook_call[1] == {
            "url": "https://user.thelearnchain.com/channels/telegram/webhook",
            "secret_token": "telegram-secret",
            "allowed_updates": ["message", "edited_message"],
            "drop_pending_updates": False,
        }

        file_bytes, media_type = await adapter.download_file("file-123")
        assert file_bytes == b"image-bytes"
        assert media_type == "image/jpeg"
    finally:
        await adapter.stop()
