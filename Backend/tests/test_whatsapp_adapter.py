from __future__ import annotations

import json
import asyncio

import httpx
import pytest

from gateway.channels.whatsapp import WhatsAppAdapter, WhatsAppConfig


def build_adapter(
    sent_payloads: list[dict[str, str]],
    *,
    text_chunk_limit: int = 4000,
    ack_delay_sec: float = 3.5,
) -> WhatsAppAdapter:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.method == "POST" and request.url.path == "/send":
            sent_payloads.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404, json={"error": "not-found"})

    client = httpx.AsyncClient(
        base_url="http://whatsapp-bridge.test",
        transport=httpx.MockTransport(handler),
    )
    config = WhatsAppConfig(
        bridge_base_url="http://whatsapp-bridge.test",
        bridge_token="bridge-token",
        text_chunk_limit=text_chunk_limit,
        ack_delay_sec=ack_delay_sec,
    )
    return WhatsAppAdapter(config, http_client=client)


@pytest.mark.asyncio
async def test_whatsapp_adapter_buffers_chunks_and_formats_final_text() -> None:
    sent_payloads: list[dict[str, str]] = []
    adapter = build_adapter(sent_payloads, ack_delay_sec=0.2)

    try:
        await adapter.send(
            {"type": "route_result", "request_id": "req_wa_1", "channel": "whatsapp:+12153079021"},
            channel="whatsapp:+12153079021",
        )
        await adapter.send(
            {
                "type": "task.created",
                "request_id": "req_wa_1",
                "task_id": "tsk_wa_1",
                "channel": "whatsapp:+12153079021",
            },
            channel="whatsapp:+12153079021",
        )
        await adapter.send(
            {
                "type": "response.thinking.chunk",
                "request_id": "req_wa_1",
                "content": "internal reasoning",
                "channel": "whatsapp:+12153079021",
            },
            channel="whatsapp:+12153079021",
        )
        await adapter.send(
            {
                "type": "task.progress",
                "request_id": "req_wa_1",
                "task_id": "tsk_wa_1",
                "payload": {"message": "Still working..."},
                "channel": "whatsapp:+12153079021",
            },
            channel="whatsapp:+12153079021",
        )
        await adapter.send(
            {
                "type": "response.chunk",
                "request_id": "req_wa_1",
                "content": "## Summary\n\n**Hello** [Site](https://example.com)\n\n- item one\n\n```python\nprint('x')\n```",
                "channel": "whatsapp:+12153079021",
            },
            channel="whatsapp:+12153079021",
        )
        await adapter.send(
            {
                "type": "response.complete",
                "request_id": "req_wa_1",
                "content": "",
                "channel": "whatsapp:+12153079021",
            },
            channel="whatsapp:+12153079021",
        )
    finally:
        await adapter.stop()

    assert sent_payloads == [
        {
            "number": "12153079021",
            "message": "*Summary*\n*Hello* Site: https://example.com\n\n• item one\n\n```\nprint('x')\n```",
        }
    ]


@pytest.mark.asyncio
async def test_whatsapp_adapter_sends_delayed_ack_for_slow_requests() -> None:
    sent_payloads: list[dict[str, str]] = []
    adapter = build_adapter(sent_payloads, ack_delay_sec=0.0)

    try:
        await adapter.send(
            {"type": "route_result", "request_id": "req_wa_slow", "channel": "whatsapp:+12153079021"},
            channel="whatsapp:+12153079021",
        )
        await asyncio.sleep(0)
        await adapter.send(
            {
                "type": "response.complete",
                "request_id": "req_wa_slow",
                "content": "Final answer",
                "channel": "whatsapp:+12153079021",
            },
            channel="whatsapp:+12153079021",
        )
    finally:
        await adapter.stop()

    assert sent_payloads == [
        {"number": "12153079021", "message": "Thinking..."},
        {"number": "12153079021", "message": "Final answer"},
    ]


@pytest.mark.asyncio
async def test_whatsapp_adapter_labels_long_multi_part_messages() -> None:
    sent_payloads: list[dict[str, str]] = []
    adapter = build_adapter(sent_payloads, text_chunk_limit=20, ack_delay_sec=0.2)

    try:
        await adapter.send(
            {
                "type": "response.complete",
                "request_id": "req_wa_long",
                "content": "First sentence. Second sentence. Third sentence.",
                "channel": "whatsapp:+12153079021",
            },
            channel="whatsapp:+12153079021",
        )
    finally:
        await adapter.stop()

    assert len(sent_payloads) >= 2
    assert sent_payloads[0]["message"].startswith("Part 1/")
    assert sent_payloads[-1]["message"].startswith(f"Part {len(sent_payloads)}/")


@pytest.mark.asyncio
async def test_whatsapp_adapter_skips_task_completed_after_response_complete() -> None:
    sent_payloads: list[dict[str, str]] = []
    adapter = build_adapter(sent_payloads, ack_delay_sec=0.2)

    try:
        await adapter.send(
            {
                "type": "response.complete",
                "request_id": "req_wa_opus",
                "content": "Final answer",
                "channel": "whatsapp:+12153079021",
            },
            channel="whatsapp:+12153079021",
        )
        await adapter.send(
            {
                "type": "task.completed",
                "request_id": "req_wa_opus",
                "task_id": "tsk_wa_opus",
                "result": {"content": "Final answer"},
                "channel": "whatsapp:+12153079021",
            },
            channel="whatsapp:+12153079021",
        )
    finally:
        await adapter.stop()

    assert sent_payloads == [{"number": "12153079021", "message": "Final answer"}]


@pytest.mark.asyncio
async def test_whatsapp_adapter_formats_markdown_tables_as_monospace_blocks() -> None:
    sent_payloads: list[dict[str, str]] = []
    adapter = build_adapter(sent_payloads, ack_delay_sec=0.2)

    try:
        await adapter.send(
            {
                "type": "response.complete",
                "request_id": "req_wa_table",
                "content": (
                    "Here is the comparison:\n\n"
                    "| Model | Speed |\n"
                    "| --- | --- |\n"
                    "| Haiku | Fast |\n"
                    "| Opus | Deep |"
                ),
                "channel": "whatsapp:+12153079021",
            },
            channel="whatsapp:+12153079021",
        )
    finally:
        await adapter.stop()

    assert sent_payloads == [
        {
            "number": "12153079021",
            "message": (
                "Here is the comparison:\n\n"
                "• Model: Haiku | Speed: Fast\n"
                "• Model: Opus | Speed: Deep"
            ),
        }
    ]
