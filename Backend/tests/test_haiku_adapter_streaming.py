from __future__ import annotations

import json

import httpx
import pytest

from gateway.adapters.haiku import HaikuAdapter
from gateway.adapters.response_processor import DirectRouteHandoff, HANDOFF_OPUS_TAG


class SSEByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return


@pytest.mark.asyncio
async def test_haiku_adapter_streams_thinking_and_text() -> None:
    events = [
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":42}}}\n\n',
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"Reasoning..."}}\n\n',
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Answer body"}}\n\n',
        b'event: message_delta\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":9}}\n\n',
        b'event: message_stop\n'
        b'data: {"type":"message_stop"}\n\n',
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["model"] == "claude-haiku-4-5"
        assert payload["thinking"] == {"type": "enabled", "budget_tokens": 10000}
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=SSEByteStream(events),
        )

    adapter = HaikuAdapter(
        api_key="anthropic-key",
        model="claude-haiku-4-5",
        anthropic_version="2023-06-01",
        max_tokens=16000,
        thinking_budget_tokens=10000,
        timeout_sec=5.0,
    )
    await adapter._client.aclose()  # noqa: SLF001
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001

    sent_events: list[dict] = []
    stored_messages: list[dict] = []
    usage_payloads: list[dict] = []

    async def send(event: dict) -> None:
        sent_events.append(event)

    async def usage_recorder(payload: dict) -> None:
        usage_payloads.append(payload)

    def store_assistant_message(content: str, *, awaiting_reply: bool, metadata, channel: str, route: str) -> None:
        stored_messages.append(
            {
                "content": content,
                "awaiting_reply": awaiting_reply,
                "metadata": metadata,
                "channel": channel,
                "route": route,
            }
        )

    try:
        await adapter.stream(
            request_id="req_haiku",
            session_id="sess_20260308",
            history=[{"role": "user", "content": "Explain blue skies."}],
            send=send,
            store_assistant_message=store_assistant_message,
            channel="desktop:desk_test",
            usage_recorder=usage_recorder,
        )
    finally:
        await adapter.close()

    assert sent_events[0]["type"] == "response.thinking.chunk"
    assert sent_events[0]["content"] == "Reasoning..."
    assert sent_events[1]["type"] == "response.chunk"
    assert sent_events[1]["content"] == "Answer body"
    assert sent_events[2]["type"] == "response.complete"
    assert sent_events[2]["route"] == "haiku"
    assert sent_events[2]["legacy_route"] == "haiku"
    assert sent_events[2]["dispatch_target"] == "direct"
    assert sent_events[2]["model_provider"] == "anthropic"
    assert sent_events[2]["model"] == "claude-haiku-4-5"
    assert sent_events[2]["thinking_text"] == "Reasoning..."
    assert sent_events[2]["metrics"]["input_tokens"] == 42
    assert sent_events[2]["metrics"]["output_tokens"] == 9
    assert len(usage_payloads) == 1
    assert usage_payloads[0]["model_key"] == "anthropic:claude-haiku-4-5"
    assert usage_payloads[0]["raw_usage"]["input_tokens"] == 42
    assert usage_payloads[0]["raw_usage"]["output_tokens"] == 9
    assert usage_payloads[0]["success"] is True

    assert stored_messages == [
        {
            "content": "Answer body",
            "awaiting_reply": False,
            "metadata": {
                "legacy_route": "haiku",
                "dispatch_target": "direct",
                "model_provider": "anthropic",
                "model": "claude-haiku-4-5",
                "preferred_model_provider": "anthropic",
                "preferred_model": "claude-haiku-4-5",
                "thinking_text": "Reasoning...",
                "stop_reason": "end_turn",
            },
            "channel": "desktop:desk_test",
            "route": "haiku",
        }
    ]


@pytest.mark.asyncio
async def test_haiku_adapter_handoff_to_opus_suppresses_direct_output() -> None:
    events = [
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":24}}}\n\n',
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"This should be discarded."}}\n\n',
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"<handoff_opus/>"}}\n\n',
        b'event: message_delta\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}\n\n',
        b'event: message_stop\n'
        b'data: {"type":"message_stop"}\n\n',
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert HANDOFF_OPUS_TAG in payload["system"]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=SSEByteStream(events),
        )

    adapter = HaikuAdapter(
        api_key="anthropic-key",
        model="claude-haiku-4-5",
        anthropic_version="2023-06-01",
        max_tokens=16000,
        thinking_budget_tokens=10000,
        timeout_sec=5.0,
    )
    await adapter._client.aclose()  # noqa: SLF001
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001

    sent_events: list[dict] = []
    stored_messages: list[dict] = []

    async def send(event: dict) -> None:
        sent_events.append(event)

    def store_assistant_message(content: str, *, awaiting_reply: bool, metadata, channel: str, route: str) -> None:
        stored_messages.append(
            {
                "content": content,
                "awaiting_reply": awaiting_reply,
                "metadata": metadata,
                "channel": channel,
                "route": route,
            }
        )

    try:
        with pytest.raises(DirectRouteHandoff) as exc_info:
            await adapter.stream(
                request_id="req_haiku_handoff",
                session_id="sess_20260308",
                history=[{"role": "user", "content": "Handle this as a real task."}],
                send=send,
                store_assistant_message=store_assistant_message,
                channel="desktop:desk_test",
            )
    finally:
        await adapter.close()

    assert exc_info.value.route == "opus"
    assert sent_events == []
    assert stored_messages == []
