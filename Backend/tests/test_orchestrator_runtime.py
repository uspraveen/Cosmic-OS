from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.runtime import ActiveTaskRun, OrchestratorRuntime
from shared import TaskEnvelope, sign_task_envelope, utcnow


class SSEByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return


class FakeRedis:
    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.groups: dict[tuple[str, str], dict[str, object]] = {}
        self._counter = 0

    async def xgroup_create(self, stream: str, group: str, id: str = "0", mkstream: bool = True) -> None:
        if mkstream:
            self.streams.setdefault(stream, [])
        self.groups.setdefault((stream, group), {"delivered": set(), "acked": set()})

    async def xadd(self, stream: str, fields: dict[str, str]) -> str:
        self._counter += 1
        message_id = f"{self._counter}-0"
        self.streams.setdefault(stream, []).append((message_id, dict(fields)))
        return message_id

    async def xreadgroup(
        self,
        *,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        del consumername
        results: list[tuple[str, list[tuple[str, dict[str, str]]]]] = []
        limit = count or 10**9
        for stream, start in streams.items():
            state = self.groups.setdefault((stream, groupname), {"delivered": set(), "acked": set()})
            delivered = state["delivered"]
            acked = state["acked"]
            messages: list[tuple[str, dict[str, str]]] = []
            for message_id, payload in self.streams.get(stream, []):
                if start == ">":
                    if message_id in delivered:
                        continue
                    delivered.add(message_id)
                    messages.append((message_id, payload))
                elif start == "0":
                    if message_id in delivered and message_id not in acked:
                        messages.append((message_id, payload))
                else:
                    raise AssertionError(f"Unsupported xreadgroup start value: {start}")
                if len(messages) >= limit:
                    break
            if messages:
                results.append((stream, messages))
        if not results and block:
            await asyncio.sleep(min(block / 1000.0, 0.02))
        return results

    async def xack(self, stream: str, group: str, message_id: str) -> int:
        state = self.groups.setdefault((stream, group), {"delivered": set(), "acked": set()})
        state["acked"].add(message_id)
        return 1

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
        b'event: content_block_start\n'
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking"}}\n\n',
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"Thinking..."}}\n\n',
        b'event: content_block_stop\n'
        b'data: {"type":"content_block_stop","index":0}\n\n',
        b'event: content_block_start\n'
        b'data: {"type":"content_block_start","index":1,"content_block":{"type":"text"}}\n\n',
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"The sky appears blue because of Rayleigh scattering."}}\n\n',
        b'event: content_block_stop\n'
        b'data: {"type":"content_block_stop","index":1}\n\n',
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
        tool_names = {tool["name"] for tool in payload["tools"]}
        assert {
            "web_search",
            "web_fetch",
            "memory_search",
            "memory_fetch",
            "memory_write",
            "memory_write_core_fact",
            "session_revisit",
            "session_history",
            "task_notebook",
        } <= tool_names
        assert "session_revisit" in payload["system"]
        assert "session_history" in payload["system"]
        assert "memory_fetch" in payload["system"]
        assert "memory_write_core_fact" in payload["system"]
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


@pytest.mark.asyncio
async def test_orchestrator_runtime_can_cancel_active_task(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger_cancel.db",
    )
    runtime = OrchestratorRuntime(config, client=client)
    task = _signed_task("signing-secret")
    created = asyncio.Event()
    streamed_events: list[dict[str, object]] = []

    async def endless_stream(
        *,
        system_prompt: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        container_id: str | None = None,
    ):
        del system_prompt, messages, tools, container_id
        if False:
            yield None
        while True:
            await asyncio.sleep(60)

    runtime._stream_anthropic_events = endless_stream  # type: ignore[method-assign]

    async def consume() -> None:
        async for event in runtime.stream_task(task):
            streamed_events.append(event)
            if event["type"] == "task.created":
                created.set()

    await runtime.start()
    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(created.wait(), timeout=2)
        assert runtime.cancel_task(task.task_id) is True
        await asyncio.wait_for(consumer, timeout=2)
    finally:
        if not consumer.done():
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        await runtime.stop()

    assert streamed_events[-1] == {
        "type": "task.cancelled",
        "task_id": "tsk_test123",
        "request_id": "req_test123",
        "session_id": "sess_20260307",
        "channel": "desktop:desk_test",
        "route": "opus",
        "status": "cancelled",
        "message": "Response stopped.",
    }


@pytest.mark.asyncio
async def test_orchestrator_runtime_emits_progress_for_memory_fetch_tool(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger_memory_fetch.db",
    )
    runtime = OrchestratorRuntime(config, client=client)
    task = _signed_task("signing-secret")
    stream_call_count = 0

    async def scripted_stream(
        *,
        system_prompt: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        container_id: str | None = None,
    ):
        del system_prompt, messages, tools, container_id
        nonlocal stream_call_count
        stream_call_count += 1
        if stream_call_count == 1:
            events = [
                ('message_start', {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
                ('content_block_start', {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "tool_mem_1", "name": "memory_fetch"}}),
                ('content_block_delta', {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{\"memory_id\":\"mem_task_1\"}"}}),
                ('content_block_stop', {"type": "content_block_stop", "index": 0}),
                ('message_delta', {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}}),
                ('message_stop', {"type": "message_stop"}),
            ]
        else:
            events = [
                ('message_start', {"type": "message_start", "message": {"usage": {"input_tokens": 12}}}),
                ('content_block_start', {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
                ('content_block_delta', {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Recovered the full memory block."}}),
                ('content_block_stop', {"type": "content_block_stop", "index": 0}),
                ('message_delta', {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}}),
                ('message_stop', {"type": "message_stop"}),
            ]
        for event_name, payload in events:
            yield type("SSE", (), {"event": event_name, "data": json.dumps(payload)})()

    async def fake_execute(
        tool_name: str,
        tool_input: dict[str, object],
        *,
        context=None,
    ) -> str:
        del context
        assert tool_name == "memory_fetch"
        assert tool_input == {"memory_id": "mem_task_1"}
        return json.dumps({"found": True, "memory_id": "mem_task_1", "content": "Full canonical body"})

    runtime._stream_anthropic_events = scripted_stream  # type: ignore[method-assign]

    await runtime.start()
    assert runtime._tool_executor is not None
    runtime._tool_executor.execute = fake_execute  # type: ignore[method-assign]
    try:
        streamed_events = [event async for event in runtime.stream_task(task)]
    finally:
        await runtime.stop()

    progress_events = [event for event in streamed_events if event["type"] == "task.progress"]
    assert any(
        event.get("tool_name") == "memory_fetch"
        and event.get("message") == "Loading full memory block mem_task_1..."
        for event in progress_events
    )
    assert any(event["type"] == "tool.call" and event["tool_name"] == "memory_fetch" for event in streamed_events)
    assert any(event["type"] == "tool.result" and event["tool_name"] == "memory_fetch" for event in streamed_events)
    assert streamed_events[-2]["type"] == "response.complete"
    assert streamed_events[-2]["content"] == "Recovered the full memory block."


@pytest.mark.asyncio
async def test_orchestrator_runtime_summarizes_parallel_local_tool_work_with_details(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger_parallel_tools.db",
    )
    runtime = OrchestratorRuntime(config, client=client)
    task = _signed_task("signing-secret")
    stream_call_count = 0

    async def scripted_stream(
        *,
        system_prompt: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        container_id: str | None = None,
    ):
        del system_prompt, messages, tools, container_id
        nonlocal stream_call_count
        stream_call_count += 1
        if stream_call_count == 1:
            events = [
                ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "tool_mem_s", "name": "memory_search"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{\"query\":\"yc\",\"max_results\":2}"}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tool_mem_f", "name": "memory_fetch"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"memory_id\":\"mem_yc_1\"}"}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 1}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}}),
                ("message_stop", {"type": "message_stop"}),
            ]
        else:
            events = [
                ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 12}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Done."}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}}),
                ("message_stop", {"type": "message_stop"}),
            ]
        for event_name, payload in events:
            yield type("SSE", (), {"event": event_name, "data": json.dumps(payload)})()

    async def fake_execute(
        tool_name: str,
        tool_input: dict[str, object],
        *,
        context=None,
    ) -> str:
        del context
        if tool_name == "memory_search":
            assert tool_input == {"query": "yc", "max_results": 2}
            return json.dumps(
                {
                    "items": [
                        {"memory_id": "mem_yc_1", "title": "YC note 1"},
                        {"memory_id": "mem_yc_2", "title": "YC note 2"},
                    ]
                }
            )
        assert tool_name == "memory_fetch"
        assert tool_input == {"memory_id": "mem_yc_1"}
        return json.dumps({"found": True, "memory_id": "mem_yc_1", "title": "Session summary sess_20260314"})

    runtime._stream_anthropic_events = scripted_stream  # type: ignore[method-assign]

    await runtime.start()
    assert runtime._tool_executor is not None
    runtime._tool_executor.execute = fake_execute  # type: ignore[method-assign]
    try:
        streamed_events = [event async for event in runtime.stream_task(task)]
    finally:
        await runtime.stop()

    progress_events = [event for event in streamed_events if event["type"] == "task.progress" and event["status"] == "tool_loop"]
    assert any(
        event["message"]
        == 'Completed parallel tool work: searched memory for "yc" and found 2 hits; loaded full memory block "Session summary sess_20260314". Continuing...'
        for event in progress_events
    )


@pytest.mark.asyncio
async def test_orchestrator_runtime_summarizes_server_side_web_search_results(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger_native_web.db",
    )
    runtime = OrchestratorRuntime(config, client=client)
    task = _signed_task("signing-secret")
    stream_call_count = 0

    async def scripted_stream(
        *,
        system_prompt: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        container_id: str | None = None,
    ):
        del system_prompt, messages, tools, container_id
        nonlocal stream_call_count
        stream_call_count += 1
        if stream_call_count == 1:
            events = [
                ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "server_tool_use", "id": "srv_web_1", "name": "web_search"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{\"query\":\"yc summer 2026 companies\"}"}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 1,
                        "content_block": {
                            "type": "web_search_tool_result",
                            "content": [
                                {
                                    "type": "web_search_result",
                                    "title": "Extruct S26 Batch",
                                    "url": "https://example.com/extruct",
                                },
                                {
                                    "type": "web_search_result",
                                    "title": "GrowthList YC S26",
                                    "url": "https://growthlist.co/yc-s26",
                                },
                            ],
                        },
                    },
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 1}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "pause_turn"}, "usage": {"output_tokens": 5}}),
                ("message_stop", {"type": "message_stop"}),
            ]
        else:
            events = [
                ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 12}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Done."}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}}),
                ("message_stop", {"type": "message_stop"}),
            ]
        for event_name, payload in events:
            yield type("SSE", (), {"event": event_name, "data": json.dumps(payload)})()

    runtime._stream_anthropic_events = scripted_stream  # type: ignore[method-assign]

    await runtime.start()
    try:
        streamed_events = [event async for event in runtime.stream_task(task)]
    finally:
        await runtime.stop()

    progress_events = [event for event in streamed_events if event["type"] == "task.progress"]
    assert any(
        event["status"] == "tool_call"
        and event.get("tool_name") == "web_search"
        and event["message"] == "Searching the web for: yc summer 2026 companies"
        for event in progress_events
    )
    assert any(
        event["status"] == "tool_loop"
        and event["message"] == "Web search found: Extruct S26 Batch (example.com), GrowthList YC S26 (growthlist.co). Continuing..."
        for event in progress_events
    )


def test_orchestrator_build_messages_includes_attachment_manifest(tmp_path) -> None:
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger_artifacts.db",
    )
    runtime = OrchestratorRuntime(config, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))))
    task = _signed_task("signing-secret").model_copy(
        update={
            "input_artifacts": [
                {
                    "artifact_id": "art_001",
                    "kind": "image",
                    "mime": "image/jpeg",
                    "filename": "photo.jpg",
                    "caption": "look at this",
                    "bridge_media_ref": "wamid_abc:att_1",
                    "download_url": "http://127.0.0.1:8091/media/wamid_abc/att_1",
                }
            ]
        }
    )

    messages = runtime._build_messages(task)  # noqa: SLF001 - direct unit seam

    assert messages[-1]["role"] == "user"
    assert "Attachment manifest:" in messages[-1]["content"]
    assert "bridge_media_ref=wamid_abc:att_1" in messages[-1]["content"]
    assert "Do not claim to have directly viewed" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_request_user_input_publishes_request_and_resumes_on_reply(tmp_path) -> None:
    fake_redis = FakeRedis()
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger_input.db",
    )
    runtime = OrchestratorRuntime(config, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))), redis_client=fake_redis)
    runtime._active_runs["tsk_waiting"] = ActiveTaskRun(
        runner_task=None,
        request_id="req_waiting",
        session_id="sess_20260312",
        channel="desktop:desk_waiting",
    )

    await runtime.start()
    try:
        request_task = asyncio.create_task(
            runtime.request_user_input(
                "tsk_waiting",
                question="Which environment should I target?",
                options=["staging", "production"],
                wait_timeout_sec=1.0,
            )
        )

        deadline = asyncio.get_running_loop().time() + 1.0
        while "user_input:requests" not in fake_redis.streams and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert "user_input:requests" in fake_redis.streams
        request_payload = json.loads(fake_redis.streams["user_input:requests"][0][1]["payload"])
        assert request_payload["task_id"] == "tsk_waiting"
        assert request_payload["channel"] == "desktop:desk_waiting"
        assert request_payload["session_id"] == "sess_20260312"
        assert request_payload["question"] == "Which environment should I target?"

        await fake_redis.xadd(
            config.task_input_replies_stream,
            {
                "payload": json.dumps(
                    {
                        "input_request_id": request_payload["input_request_id"],
                        "task_id": "tsk_waiting",
                        "content": "Use staging.",
                        "channel": "desktop:desk_waiting",
                        "timestamp": "2026-03-12T12:00:00Z",
                    }
                )
            },
        )
        result = await asyncio.wait_for(request_task, timeout=2.0)

        assert result["status"] == "answered"
        assert result["reply"]["content"] == "Use staging."

        with sqlite3.connect(config.task_ledger_db_path) as connection:
            row = connection.execute(
                "SELECT status, reply_content FROM task_input_requests WHERE input_request_id = ?",
                (request_payload["input_request_id"],),
            ).fetchone()
        assert row == ("answered", "Use staging.")
    finally:
        await runtime.stop()
