from __future__ import annotations

import json

import httpx
import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.runtime import OrchestratorRuntime
from shared import TaskEnvelope, sign_task_envelope, utcnow


class SSEByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return


def _signed_task(signing_secret: str) -> TaskEnvelope:
    task = TaskEnvelope(
        task_id="tsk_test123",
        task_list_id="sess_20260307",
        session_id="sess_20260307",
        sender="cosmic/gateway:1.0.0",
        recipient="cosmic/orchestrator:1.0.0",
        intent="orchestrator.process",
        input={
            "query": "Why is the sky blue?",
            "request_id": "req_test123",
            "conversation_context": [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello"},
            ],
        },
        idempotency_key="idem_test123",
        priority="high",
        signature="",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:desk_test",
    )
    return task.model_copy(update={"signature": sign_task_envelope(task, signing_secret)})


@pytest.mark.asyncio
async def test_orchestrator_runtime_streams_thinking_and_text(tmp_path) -> None:
    events = [
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":123}}}\n\n',
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"Thinking..."}}\n\n',
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"The sky appears blue because of Rayleigh scattering."}}\n\n',
        b'event: message_delta\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":27}}\n\n',
        b"event: message_stop\n"
        b'data: {"type":"message_stop"}\n\n',
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://api.anthropic.com/v1/messages")
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["model"] == "claude-opus-4-6"
        assert payload["thinking"] == {"type": "adaptive"}
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=SSEByteStream(events),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger.db",
    )
    runtime = OrchestratorRuntime(config, client=client)
    await runtime.start()
    try:
        task = _signed_task("signing-secret")
        streamed_events = [event async for event in runtime.stream_task(task)]
    finally:
        await runtime.stop()

    assert streamed_events[0]["type"] == "task.created"
    assert streamed_events[1]["type"] == "task.progress"
    assert streamed_events[1]["status"] == "thinking"
    assert streamed_events[2]["type"] == "response.thinking.chunk"
    assert streamed_events[2]["content"] == "Thinking..."
    assert streamed_events[3]["type"] == "task.progress"
    assert streamed_events[3]["status"] == "responding"
    assert streamed_events[4]["type"] == "response.chunk"
    assert "Rayleigh scattering" in streamed_events[4]["content"]
    assert streamed_events[5]["type"] == "response.complete"
    assert streamed_events[5]["route"] == "opus"
    assert streamed_events[5]["thinking_text"] == "Thinking..."
    assert streamed_events[5]["metrics"]["input_tokens"] == 123
    assert streamed_events[5]["metrics"]["output_tokens"] == 27
    assert streamed_events[6] == {
        "type": "task.completed",
        "task_id": "tsk_test123",
        "request_id": "req_test123",
        "session_id": "sess_20260307",
        "channel": "desktop:desk_test",
        "route": "opus",
        "status": "completed",
    }
