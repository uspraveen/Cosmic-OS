from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from shutil import rmtree
from uuid import uuid4

import httpx
import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.prompts import build_agentic_system_prompt, get_prompt_asset_hashes
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


_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc`\x00\x00"
    b"\x00\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
)


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


def _fake_sse(event: str, payload: dict[str, object]) -> object:
    return type("SSE", (), {"event": event, "data": json.dumps(payload)})()


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
            "agent_catalog_search",
            "delegate_to_agent",
            "cosmics_capability_wishlist_search",
            "cosmics_capability_wishlist_capture",
            "memory_search",
            "memory_fetch",
            "memory_write",
            "memory_write_core_fact",
            "session_revisit",
            "session_history",
            "task_notebook",
        } <= tool_names
        assert {
            "docs_browse",
            "docs_search",
            "docs_read",
            "docs_fetch_asset",
            "docs_reinspect_asset",
            "sheets_browse",
            "sheets_schema",
            "sheets_preview",
            "sheets_query",
            "sheets_export",
            "sheets_create_workbook",
            "sheets_create_sheet",
            "firecrawl_scrape",
            "firecrawl_extract",
            "firecrawl_recall_session",
            "x_search",
            "x_recall_session",
        }.isdisjoint(tool_names)
        assert "session_revisit" in payload["system"]
        assert "session_history" in payload["system"]
        assert "memory_fetch" in payload["system"]
        assert "memory_write_core_fact" in payload["system"]
        assert "agent_catalog_search" in payload["system"]
        assert "delegate_to_agent" in payload["system"]
        assert "docs_browse" not in payload["system"]
        assert "sheets_browse" not in payload["system"]
        assert "cosmics_capability_wishlist_search" in payload["system"]
        assert "cosmics_capability_wishlist_capture" in payload["system"]
        assert "firecrawl_extract" not in payload["system"]
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

@pytest.mark.asyncio
async def test_orchestrator_runtime_streams_fireworks_kimi_path() -> None:
    runtime_root = Path.cwd() / "pytest-kimi-runtime" / uuid4().hex
    runtime_root.mkdir(parents=True, exist_ok=False)
    chunks = [
        b'data: {"choices":[{"delta":{"reasoning_content":"Planning..."},"finish_reason":null}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"Hello from Kimi."},"finish_reason":null}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":4,"total_tokens":14,"prompt_tokens_details":{"cached_tokens":2},"completion_tokens_details":{"reasoning_tokens":1}}}\n\n',
        b"data: [DONE]\n\n",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/chat/completions"):
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["model"] == "accounts/fireworks/models/kimi-k2p6"
            assert payload["messages"][0]["role"] == "system"
            assert "max_tokens" not in payload
            tool_names = {
                tool["function"]["name"]
                for tool in payload.get("tools", [])
                if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
            }
            assert "perplexity_research" in tool_names
            assert "web_search" not in tool_names
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=SSEByteStream(chunks),
            )
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="",
        fireworks_api_key="fireworks-key",
        task_ledger_db_path=runtime_root / "task_ledger_kimi.db",
    )
    runtime = OrchestratorRuntime(config, client=client)
    await runtime.start()
    try:
        base_task = _signed_task("signing-secret")
        task = base_task.model_copy(
            update={
                "input": {
                    **base_task.input,
                    "cosmic_orchestrator_model": {
                        "provider": "fireworks_kimi",
                        "model": "accounts/fireworks/models/kimi-k2p6",
                    },
                }
            }
        )
        task = task.model_copy(update={"signature": sign_task_envelope(task, "signing-secret")})
        streamed_events = [event async for event in runtime.stream_task(task)]
    finally:
        await runtime.stop()
        rmtree(runtime_root, ignore_errors=True)

    assert streamed_events[0]["type"] == "task.created"
    assert streamed_events[0]["model_provider"] == "fireworks_kimi"
    assert any(
        event["type"] == "response.thinking.chunk" and event["content"] == "Planning..."
        for event in streamed_events
    )
    complete = next(event for event in streamed_events if event["type"] == "response.complete")
    assert complete["content"] == "Hello from Kimi."
    assert complete["model_provider"] == "fireworks_kimi"
    assert complete["metrics"]["prompt_tokens"] == 10
    assert complete["metrics"]["cached_tokens"] == 2
    assert complete["metrics"]["reasoning_tokens"] == 1


@pytest.mark.asyncio
async def test_orchestrator_runtime_separates_fireworks_inline_thinking() -> None:
    runtime_root = Path.cwd() / "pytest-kimi-runtime" / uuid4().hex
    runtime_root.mkdir(parents=True, exist_ok=False)
    chunks = [
        b'data: {"choices":[{"delta":{"content":"<thi"},"finish_reason":null}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"nk>Hidden plan."},"finish_reason":null}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"</thi"},"finish_reason":null}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"nk>Visible answer."},"finish_reason":null}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":4,"total_tokens":14}}\n\n',
        b"data: [DONE]\n\n",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/chat/completions"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=SSEByteStream(chunks),
            )
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="",
        fireworks_api_key="fireworks-key",
        task_ledger_db_path=runtime_root / "task_ledger_kimi.db",
    )
    runtime = OrchestratorRuntime(config, client=client)
    await runtime.start()
    try:
        base_task = _signed_task("signing-secret")
        task = base_task.model_copy(
            update={
                "input": {
                    **base_task.input,
                    "cosmic_orchestrator_model": {
                        "provider": "fireworks_kimi",
                        "model": "accounts/fireworks/models/kimi-k2p6",
                    },
                }
            }
        )
        task = task.model_copy(update={"signature": sign_task_envelope(task, "signing-secret")})
        streamed_events = [event async for event in runtime.stream_task(task)]
    finally:
        await runtime.stop()
        rmtree(runtime_root, ignore_errors=True)

    thinking_text = "".join(
        str(event.get("content") or "")
        for event in streamed_events
        if event["type"] == "response.thinking.chunk"
    )
    complete = next(event for event in streamed_events if event["type"] == "response.complete")
    assert thinking_text == "Hidden plan."
    assert complete["thinking_text"] == "Hidden plan."
    assert complete["content"] == "Visible answer."


def test_collect_specialist_artifacts_only_keeps_deliverables(tmp_path) -> None:
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger.db",
    )
    runtime = OrchestratorRuntime(config)
    produced_artifacts: list[dict[str, object]] = []

    runtime._collect_specialist_artifacts(
        json.dumps(
            {
                "artifacts": [
                    {
                        "artifact_id": "art_supporting_1",
                        "task_id": "tsk_supporting",
                        "mime": "application/json",
                        "path": "runs/artifacts/tsk_supporting/supporting.json",
                        "filename": "supporting.json",
                        "kind": "output",
                        "audience": "supporting",
                    },
                    {
                        "artifact_id": "art_deliverable_1",
                        "task_id": "tsk_deliverable",
                        "mime": "text/csv",
                        "path": "runs/artifacts/tsk_deliverable/output.csv",
                        "filename": "output.csv",
                        "kind": "output",
                        "audience": "deliverable",
                    },
                ]
            }
        ),
        produced_artifacts=produced_artifacts,
    )

    assert produced_artifacts == [
        {
            "artifact_id": "art_deliverable_1",
            "task_id": "tsk_deliverable",
            "mime": "text/csv",
            "path": "runs/artifacts/tsk_deliverable/output.csv",
            "kind": "output",
            "audience": "deliverable",
            "filename": "output.csv",
        }
    ]


@pytest.mark.asyncio
async def test_orchestrator_runtime_promotes_anthropic_generated_files_to_produced_artifacts(tmp_path) -> None:
    first_turn_events = [
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":42}}}\n\n',
        b'event: content_block_start\n'
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"server_tool_use","id":"srvtoolu_123","name":"code_execution"}}\n\n',
        b'event: content_block_stop\n'
        b'data: {"type":"content_block_stop","index":0}\n\n',
        b'event: content_block_start\n'
        b'data: {"type":"content_block_start","index":1,"content_block":{"type":"bash_code_execution_tool_result","tool_use_id":"srvtoolu_123","content":{"type":"bash_code_execution_result","stdout":"","stderr":"","return_code":0,"content":[{"type":"file","file_id":"file_chart_png"}]}}}\n\n',
        b'event: content_block_stop\n'
        b'data: {"type":"content_block_stop","index":1}\n\n',
        b'event: message_delta\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"pause_turn"},"usage":{"output_tokens":11}}\n\n',
        b"event: message_stop\n"
        b'data: {"type":"message_stop"}\n\n',
    ]
    second_turn_events = [
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":7}}}\n\n',
        b'event: content_block_start\n'
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}\n\n',
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Here is the chart."}}\n\n',
        b'event: content_block_stop\n'
        b'data: {"type":"content_block_stop","index":0}\n\n',
        b'event: message_delta\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":9}}\n\n',
        b"event: message_stop\n"
        b'data: {"type":"message_stop"}\n\n',
    ]
    message_requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal message_requests
        if request.url == httpx.URL("https://api.anthropic.com/v1/messages"):
            message_requests += 1
            assert request.headers["anthropic-beta"] == "code-execution-2025-05-22,files-api-2025-04-14"
            stream = first_turn_events if message_requests == 1 else second_turn_events
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=SSEByteStream(stream),
            )
        if request.url == httpx.URL("https://api.anthropic.com/v1/files/file_chart_png"):
            assert request.headers["anthropic-beta"] == "files-api-2025-04-14"
            return httpx.Response(
                200,
                json={
                    "id": "file_chart_png",
                    "filename": "YC_P26_Categories.png",
                    "mime_type": "image/png",
                    "created_at": "2026-03-28T02:00:00Z",
                },
            )
        if request.url == httpx.URL("https://api.anthropic.com/v1/files/file_chart_png/content"):
            assert request.headers["anthropic-beta"] == "files-api-2025-04-14"
            return httpx.Response(
                200,
                headers={"content-type": "image/png"},
                content=b"PNGDATA",
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = OrchestratorConfig(
        artifacts_root=tmp_path / "artifacts",
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        anthropic_files_api_beta="files-api-2025-04-14",
        task_ledger_db_path=tmp_path / "task_ledger.db",
    )
    runtime = OrchestratorRuntime(config, client=client)
    await runtime.start()
    try:
        task = _signed_task("signing-secret")
        streamed_events = [event async for event in runtime.stream_task(task)]
    finally:
        await runtime.stop()

    complete_event = next(event for event in streamed_events if event["type"] == "response.complete")
    assert complete_event["content"] == "Here is the chart."
    assert complete_event["produced_artifacts"] == [
        {
            "artifact_id": "anthropic_file_chart_png",
            "task_id": "tsk_test123",
            "mime": "image/png",
            "path": "runs/artifacts/tsk_test123/orchestrator/anthropic_code_execution/file_chart_png__YC_P26_Categories.png",
            "kind": "output",
            "audience": "deliverable",
            "filename": "YC_P26_Categories.png",
            "created_by_agent": "cosmic/orchestrator:1.0.0",
            "sha256": hashlib.sha256(b"PNGDATA").hexdigest(),
            "created_at": "2026-03-28T02:00:00Z",
        }
    ]
    persisted_path = tmp_path / "artifacts" / "tsk_test123" / "orchestrator" / "anthropic_code_execution" / "file_chart_png__YC_P26_Categories.png"
    assert persisted_path.read_bytes() == b"PNGDATA"
    assert streamed_events[4]["type"] == "response.chunk"
    assert "Rayleigh scattering" in streamed_events[4]["content"]
    assert streamed_events[5]["type"] == "response.complete"
    assert streamed_events[5]["route"] == "opus"
    assert streamed_events[5]["thinking_text"] == "Thinking..."
    assert streamed_events[5]["metrics"]["input_tokens"] == 123
    assert streamed_events[5]["metrics"]["output_tokens"] == 27
    assert streamed_events[5]["metrics"]["anthropic_requests"] == 1
    assert streamed_events[5]["metrics"]["container_captured"] is False
    assert streamed_events[5]["metrics"]["container_reuse_turns"] == 0
    assert streamed_events[5]["metrics"]["max_request_message_count"] == 3
    assert streamed_events[5]["metrics"]["max_request_context_chars"] > 0
    assert streamed_events[6] == {
        "type": "task.completed",
        "task_id": "tsk_test123",
        "request_id": "req_test123",
        "session_id": "sess_20260307",
        "channel": "desktop:desk_test",
        "route": "opus",
        "status": "completed",
    }

    with sqlite3.connect(tmp_path / "task_ledger.db") as connection:
        row = connection.execute(
            "SELECT result_json FROM tasks WHERE task_id = ?",
            ("tsk_test123",),
        ).fetchone()
    assert row is not None
    result_payload = json.loads(row[0])
    assert result_payload["loop_diagnostics"] == {
        "anthropic_requests": 1,
        "container_captured": False,
        "container_reuse_turns": 0,
        "max_request_context_chars": streamed_events[5]["metrics"]["max_request_context_chars"],
        "max_request_message_count": 3,
    }


@pytest.mark.asyncio
async def test_orchestrator_runtime_can_enable_anthropic_prompt_cache(tmp_path) -> None:
    observed_payloads: list[dict[str, object]] = []
    events = [
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":19,"cache_creation_input_tokens":1400,"cache_read_input_tokens":0}}}\n\n',
        b'event: content_block_start\n'
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}\n\n',
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Cached prompt path is configured."}}\n\n',
        b'event: content_block_stop\n'
        b'data: {"type":"content_block_stop","index":0}\n\n',
        b'event: message_delta\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":9}}\n\n',
        b"event: message_stop\n"
        b'data: {"type":"message_stop"}\n\n',
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        observed_payloads.append(payload)
        system_blocks = payload["system"]
        assert isinstance(system_blocks, list)
        assert system_blocks == [
            {
                "type": "text",
                "text": system_blocks[0]["text"],
                "cache_control": {"type": "ephemeral"},
            }
        ]
        assert "session_history" in system_blocks[0]["text"]
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
        anthropic_prompt_cache_enabled=True,
        task_ledger_db_path=tmp_path / "task_ledger_prompt_cache.db",
    )
    runtime = OrchestratorRuntime(config, client=client)
    await runtime.start()
    try:
        streamed_events = [event async for event in runtime.stream_task(_signed_task("signing-secret"))]
    finally:
        await runtime.stop()

    assert len(observed_payloads) == 1
    complete_event = next(event for event in streamed_events if event["type"] == "response.complete")
    assert complete_event["content"] == "Cached prompt path is configured."
    assert complete_event["metrics"]["cache_creation_input_tokens"] == 1400
    assert complete_event["metrics"]["cache_read_input_tokens"] == 0


@pytest.mark.asyncio
async def test_orchestrator_runtime_reuses_container_without_inlining_system_prompt(tmp_path) -> None:
    request_payloads: list[dict[str, object]] = []

    def system_prompt_leaks_into_messages(payload: dict[str, object]) -> bool:
        system_prompt = str(payload.get("system") or "")
        if not system_prompt:
            return False
        for message in payload.get("messages", []):
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str) and system_prompt in content:
                return True
            if not isinstance(content, str):
                try:
                    rendered = json.dumps(content, ensure_ascii=False, sort_keys=True)
                except (TypeError, ValueError):
                    rendered = repr(content)
                if system_prompt in rendered:
                    return True
        return False

    first_events = [
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":11},"container":{"id":"cont_loop_123"}}}\n\n',
        b'event: content_block_start\n'
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking"}}\n\n',
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"Need to search memory."}}\n\n',
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"sig_loop_1"}}\n\n',
        b'event: content_block_stop\n'
        b'data: {"type":"content_block_stop","index":0}\n\n',
        b'event: content_block_start\n'
        b'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"tool_mem_1","name":"memory_search"}}\n\n',
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"query\\":\\"yc\\",\\"max_results\\":1}"}}\n\n',
        b'event: content_block_stop\n'
        b'data: {"type":"content_block_stop","index":1}\n\n',
        b'event: message_delta\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":6}}\n\n',
        b"event: message_stop\n"
        b'data: {"type":"message_stop"}\n\n',
    ]
    second_events = [
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":14}}}\n\n',
        b'event: content_block_start\n'
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}\n\n',
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"I checked the memory hit."}}\n\n',
        b'event: content_block_stop\n'
        b'data: {"type":"content_block_stop","index":0}\n\n',
        b'event: message_delta\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":8}}\n\n',
        b"event: message_stop\n"
        b'data: {"type":"message_stop"}\n\n',
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://api.anthropic.com/v1/messages")
        payload = json.loads(request.content.decode("utf-8"))
        request_payloads.append(payload)
        assert system_prompt_leaks_into_messages(payload) is False

        if len(request_payloads) == 1:
            assert "container" not in payload
            assert payload["messages"] == [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello"},
                {"role": "user", "content": "Why is the sky blue?"},
            ]
            stream = SSEByteStream(first_events)
        else:
            assert payload["container"] == "cont_loop_123"
            assistant_message = payload["messages"][-2]
            assert assistant_message == {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Need to search memory.", "signature": "sig_loop_1"},
                    {"type": "tool_use", "id": "tool_mem_1", "name": "memory_search", "input": {"query": "yc", "max_results": 1}},
                ],
            }
            tool_result_message = payload["messages"][-1]
            assert tool_result_message["role"] == "user"
            assert tool_result_message["content"] == [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool_mem_1",
                    "content": json.dumps({"items": [{"memory_id": "mem_yc_1", "title": "YC summary"}]}),
                }
            ]
            stream = SSEByteStream(second_events)

        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger_loop_reuse.db",
    )
    runtime = OrchestratorRuntime(config, client=client)

    async def fake_execute(
        tool_name: str,
        tool_input: dict[str, object],
        *,
        context=None,
    ) -> str:
        del context
        assert tool_name == "memory_search"
        assert tool_input == {"query": "yc", "max_results": 1}
        return json.dumps({"items": [{"memory_id": "mem_yc_1", "title": "YC summary"}]})

    await runtime.start()
    assert runtime._tool_executor is not None
    runtime._tool_executor.execute = fake_execute  # type: ignore[method-assign]
    try:
        streamed_events = [event async for event in runtime.stream_task(_signed_task("signing-secret"))]
        loop_snapshot = runtime.get_loop_diagnostics_snapshot()
    finally:
        await runtime.stop()

    assert len(request_payloads) == 2
    complete_event = next(event for event in streamed_events if event["type"] == "response.complete")
    assert complete_event["metrics"]["anthropic_requests"] == 2
    assert complete_event["metrics"]["container_captured"] is True
    assert complete_event["metrics"]["container_reuse_turns"] == 1
    assert complete_event["metrics"]["max_request_message_count"] == 5
    assert complete_event["metrics"]["max_request_context_chars"] > 0
    assert loop_snapshot == {
        "anthropic_requests": 2,
        "tasks_observed": 1,
        "tasks_with_tool_loops": 1,
        "tasks_with_container_capture": 1,
        "container_reuse_turns": 1,
        "max_request_context_chars": complete_event["metrics"]["max_request_context_chars"],
        "max_request_message_count": 5,
        "max_tool_iterations": 2,
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
        assert await runtime.cancel_task(task.task_id) is True
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
        usage_context: dict[str, object] | None = None,
        model_override: str | None = None,
    ):
        del system_prompt, messages, tools, container_id, usage_context, model_override
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
        usage_context: dict[str, object] | None = None,
        model_override: str | None = None,
    ):
        del system_prompt, messages, tools, container_id, usage_context, model_override
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
        usage_context: dict[str, object] | None = None,
        model_override: str | None = None,
    ):
        del system_prompt, messages, tools, container_id, usage_context, model_override
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
    complete_event = next(event for event in streamed_events if event["type"] == "response.complete")
    assert complete_event["research_provenance"] == {
        "paths": ["native_web_search"],
        "source_count": 2,
        "source_domains": ["example.com", "growthlist.co"],
        "source_sample": [
            {
                "url": "https://example.com/extruct",
                "title": "Extruct S26 Batch",
                "domain": "example.com",
            },
            {
                "url": "https://growthlist.co/yc-s26",
                "title": "GrowthList YC S26",
                "domain": "growthlist.co",
            },
        ],
    }


@pytest.mark.asyncio
async def test_orchestrator_runtime_replays_bash_code_execution_result_blocks(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger_bash_pause.db",
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
        usage_context: dict[str, object] | None = None,
        model_override: str | None = None,
    ):
        del system_prompt, tools, container_id, usage_context, model_override
        nonlocal stream_call_count
        stream_call_count += 1
        if stream_call_count == 1:
            events = [
                ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 9}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "server_tool_use", "id": "srv_bash_1", "name": "bash_code_execution"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{\"command\":\"echo hi\"}"}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 1,
                        "content_block": {
                            "type": "bash_code_execution_tool_result",
                            "tool_use_id": "srv_bash_1",
                            "stdout": "hi\n",
                            "stderr": "",
                            "exit_code": 0,
                        },
                    },
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 1}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "pause_turn"}, "usage": {"output_tokens": 4}}),
                ("message_stop", {"type": "message_stop"}),
            ]
        else:
            assistant_message = messages[-1]
            assert assistant_message == {
                "role": "assistant",
                "content": [
                    {
                        "type": "server_tool_use",
                        "id": "srv_bash_1",
                        "name": "bash_code_execution",
                        "input": {"command": "echo hi"},
                    },
                    {
                        "type": "bash_code_execution_tool_result",
                        "tool_use_id": "srv_bash_1",
                        "stdout": "hi\n",
                        "stderr": "",
                        "exit_code": 0,
                    },
                ],
            }
            events = [
                ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 11}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Finished."}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 6}}),
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

    complete_event = next(event for event in streamed_events if event["type"] == "response.complete")
    assert complete_event["content"] == "Finished."


@pytest.mark.asyncio
async def test_orchestrator_runtime_replays_code_execution_result_blocks(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger_code_exec_pause.db",
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
        usage_context: dict[str, object] | None = None,
        model_override: str | None = None,
    ):
        del system_prompt, tools, container_id, usage_context, model_override
        nonlocal stream_call_count
        stream_call_count += 1
        if stream_call_count == 1:
            events = [
                ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 9}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "server_tool_use", "id": "srv_code_1", "name": "code_execution"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{\"code\":\"print(1)\"}"}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 1,
                        "content_block": {
                            "type": "code_execution_tool_result",
                            "tool_use_id": "srv_code_1",
                            "content": [{"type": "code_execution_result", "stdout": "1\n"}],
                        },
                    },
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 1}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "pause_turn"}, "usage": {"output_tokens": 4}}),
                ("message_stop", {"type": "message_stop"}),
            ]
        else:
            assistant_message = messages[-1]
            assert assistant_message == {
                "role": "assistant",
                "content": [
                    {
                        "type": "server_tool_use",
                        "id": "srv_code_1",
                        "name": "code_execution",
                        "input": {"code": "print(1)"},
                    },
                    {
                        "type": "code_execution_tool_result",
                        "tool_use_id": "srv_code_1",
                        "content": [{"type": "code_execution_result", "stdout": "1\n"}],
                    },
                ],
            }
            events = [
                ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 11}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Finished."}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 6}}),
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

    complete_event = next(event for event in streamed_events if event["type"] == "response.complete")
    assert complete_event["content"] == "Finished."


@pytest.mark.asyncio
async def test_orchestrator_runtime_skips_unmatched_server_tool_use_blocks_on_pause_turn(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger_code_exec_unmatched.db",
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
        usage_context: dict[str, object] | None = None,
        model_override: str | None = None,
    ):
        del system_prompt, tools, container_id, usage_context, model_override
        nonlocal stream_call_count
        stream_call_count += 1
        if stream_call_count == 1:
            events = [
                ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 9}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Working..."}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "server_tool_use", "id": "srv_code_2", "name": "code_execution"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"code\":\"print(2)\"}"}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 1}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "pause_turn"}, "usage": {"output_tokens": 4}}),
                ("message_stop", {"type": "message_stop"}),
            ]
        else:
            assistant_message = messages[-1]
            assert assistant_message == {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Working..."},
                ],
            }
            events = [
                ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 11}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Recovered."}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 6}}),
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

    progress_event = next(
        event
        for event in streamed_events
        if event["type"] == "task.progress" and event["status"] == "tool_loop"
    )
    assert "skipped" in progress_event["message"].lower()
    complete_event = next(event for event in streamed_events if event["type"] == "response.complete")
    assert complete_event["content"] == "Working...Recovered."


@pytest.mark.asyncio
async def test_orchestrator_runtime_skips_incomplete_code_execution_result_blocks_on_pause_turn(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger_code_exec_incomplete.db",
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
        usage_context: dict[str, object] | None = None,
        model_override: str | None = None,
    ):
        del system_prompt, tools, container_id, usage_context, model_override
        nonlocal stream_call_count
        stream_call_count += 1
        if stream_call_count == 1:
            events = [
                ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 9}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Working..."}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "server_tool_use", "id": "srv_code_3", "name": "code_execution"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"code\":\"print(3)\"}"}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 1}),
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 2,
                        "content_block": {
                            "type": "code_execution_tool_result",
                            "tool_use_id": "srv_code_3",
                        },
                    },
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 2}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "pause_turn"}, "usage": {"output_tokens": 4}}),
                ("message_stop", {"type": "message_stop"}),
            ]
        else:
            assistant_message = messages[-1]
            assert assistant_message == {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Working..."},
                ],
            }
            events = [
                ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 11}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Recovered."}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 6}}),
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

    progress_event = next(
        event
        for event in streamed_events
        if event["type"] == "task.progress" and event["status"] == "tool_loop"
    )
    assert "skipped" in progress_event["message"].lower()
    complete_event = next(event for event in streamed_events if event["type"] == "response.complete")
    assert complete_event["content"] == "Working...Recovered."


@pytest.mark.asyncio
async def test_orchestrator_runtime_sanitizes_incomplete_server_tool_blocks_before_local_tool_replay(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger_code_exec_incomplete_local_tool.db",
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
        usage_context: dict[str, object] | None = None,
        model_override: str | None = None,
    ):
        del system_prompt, tools, container_id, usage_context, model_override
        nonlocal stream_call_count
        stream_call_count += 1
        if stream_call_count == 1:
            events = [
                ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Researching..."}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "server_tool_use", "id": "srv_code_mix", "name": "code_execution"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"code\":\"print(7)\"}"}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 1}),
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 2,
                        "content_block": {
                            "type": "code_execution_tool_result",
                            "tool_use_id": "srv_code_mix",
                        },
                    },
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 2}),
                ("content_block_start", {"type": "content_block_start", "index": 3, "content_block": {"type": "tool_use", "id": "tool_mem_1", "name": "memory_search"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 3, "delta": {"type": "input_json_delta", "partial_json": "{\"query\":\"tn election\",\"max_results\":1}"}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 3}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}}),
                ("message_stop", {"type": "message_stop"}),
            ]
        else:
            assistant_message = messages[-2]
            assert assistant_message == {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Researching..."},
                    {
                        "type": "tool_use",
                        "id": "tool_mem_1",
                        "name": "memory_search",
                        "input": {"query": "tn election", "max_results": 1},
                    },
                ],
            }
            tool_result_message = messages[-1]
            assert tool_result_message == {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool_mem_1",
                        "content": json.dumps({"items": [{"memory_id": "mem_tn_1", "title": "Tamil Nadu note"}]}),
                    }
                ],
            }
            events = [
                ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 12}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Recovered after local tool."}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 6}}),
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
        assert tool_name == "memory_search"
        assert tool_input == {"query": "tn election", "max_results": 1}
        return json.dumps({"items": [{"memory_id": "mem_tn_1", "title": "Tamil Nadu note"}]})

    runtime._stream_anthropic_events = scripted_stream  # type: ignore[method-assign]

    await runtime.start()
    assert runtime._tool_executor is not None
    runtime._tool_executor.execute = fake_execute  # type: ignore[method-assign]
    try:
        streamed_events = [event async for event in runtime.stream_task(task)]
    finally:
        await runtime.stop()

    progress_event = next(
        event
        for event in streamed_events
        if event["type"] == "task.progress" and event["status"] == "tool_loop"
    )
    assert "skipped" in progress_event["message"].lower()
    complete_event = next(event for event in streamed_events if event["type"] == "response.complete")
    assert complete_event["content"] == "Researching...Recovered after local tool."


@pytest.mark.asyncio
async def test_orchestrator_runtime_sanitizes_prior_assistant_server_tool_blocks_before_request(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger_prior_replay_sanitized.db",
    )
    runtime = OrchestratorRuntime(config, client=client)
    base_task = _signed_task("signing-secret")
    task = base_task.model_copy(
        update={
            "input": {
                **dict(base_task.input),
                "conversation_context": [
                    {"role": "user", "content": "Earlier question"},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Working..."},
                            {
                                "type": "server_tool_use",
                                "id": "srv_code_prior",
                                "name": "code_execution",
                                "input": {"code": "print(9)"},
                            },
                        ],
                    },
                ],
            }
        }
    )
    task = task.model_copy(update={"signature": sign_task_envelope(task, "signing-secret")})

    async def scripted_stream(
        *,
        system_prompt: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        container_id: str | None = None,
        usage_context: dict[str, object] | None = None,
        model_override: str | None = None,
    ):
        del system_prompt, tools, container_id, usage_context, model_override
        assert messages == [
            {"role": "user", "content": "Earlier question"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Working..."},
                ],
            },
            {"role": "user", "content": "Why is the sky blue?"},
        ]
        events = [
            ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 7}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Recovered cleanly."}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}}),
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

    complete_event = next(event for event in streamed_events if event["type"] == "response.complete")
    assert complete_event["content"] == "Recovered cleanly."


@pytest.mark.asyncio
async def test_orchestrator_runtime_recovers_from_provider_server_tool_replay_error(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger_server_tool_replay_recovery.db",
    )
    runtime = OrchestratorRuntime(config, client=client)
    base_task = _signed_task("signing-secret")
    task = base_task.model_copy(
        update={
            "input": {
                **dict(base_task.input),
                "conversation_context": [
                    {"role": "user", "content": "Earlier question"},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Working..."},
                            {
                                "type": "server_tool_use",
                                "id": "srv_code_hist",
                                "name": "code_execution",
                                "input": {"code": "print(1)"},
                            },
                            {
                                "type": "code_execution_tool_result",
                                "tool_use_id": "srv_code_hist",
                                "content": [{"type": "text", "text": "1"}],
                            },
                        ],
                    },
                ],
            }
        }
    )
    task = task.model_copy(update={"signature": sign_task_envelope(task, "signing-secret")})
    stream_call_count = 0

    async def scripted_stream(
        *,
        system_prompt: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        container_id: str | None = None,
        usage_context: dict[str, object] | None = None,
        model_override: str | None = None,
    ):
        del system_prompt, tools, container_id, usage_context, model_override
        nonlocal stream_call_count
        stream_call_count += 1
        assert messages == [
            {"role": "user", "content": "Earlier question"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Working..."},
                ],
            },
            {"role": "user", "content": "Why is the sky blue?"},
        ]
        if stream_call_count == 1:
            raise RuntimeError(
                "Anthropic API error: messages.1: code_execution tool use with id "
                "srv_code_hist was found without a corresponding code_execution_tool_result block"
            )

        for event_name, payload in [
            ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 7}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Recovered after retry."}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}}),
            ("message_stop", {"type": "message_stop"}),
        ]:
            yield type("SSE", (), {"event": event_name, "data": json.dumps(payload)})()

    runtime._stream_anthropic_events = scripted_stream  # type: ignore[method-assign]

    await runtime.start()
    try:
        streamed_events = [event async for event in runtime.stream_task(task)]
    finally:
        await runtime.stop()

    retry_event = next(
        event
        for event in streamed_events
        if event["type"] == "task.progress" and event["status"] == "retrying"
    )
    assert "recovering and retrying" in retry_event["message"].lower()
    complete_event = next(event for event in streamed_events if event["type"] == "response.complete")
    assert complete_event["content"] == "Recovered after retry."
    assert stream_call_count == 2


@pytest.mark.asyncio
async def test_orchestrator_runtime_recovers_from_modified_thinking_replay_error(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger_thinking_replay_recovery.db",
    )
    runtime = OrchestratorRuntime(config, client=client)
    base_task = _signed_task("signing-secret")
    task = base_task.model_copy(
        update={
            "input": {
                **dict(base_task.input),
                "conversation_context": [
                    {"role": "user", "content": "Earlier question"},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "I need a web fetch.", "signature": "sig_hist"},
                            {"type": "text", "text": "Working..."},
                            {
                                "type": "server_tool_use",
                                "id": "srv_fetch_hist",
                                "name": "web_fetch",
                                "input": {"url": "https://example.com"},
                            },
                            {
                                "type": "web_fetch_tool_result",
                                "tool_use_id": "srv_fetch_hist",
                                "content": [{"type": "text", "text": "Example"}],
                            },
                        ],
                    },
                ],
            }
        }
    )
    task = task.model_copy(update={"signature": sign_task_envelope(task, "signing-secret")})
    stream_call_count = 0

    async def scripted_stream(
        *,
        system_prompt: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        container_id: str | None = None,
        usage_context: dict[str, object] | None = None,
        model_override: str | None = None,
    ):
        del system_prompt, tools, container_id, usage_context, model_override
        nonlocal stream_call_count
        stream_call_count += 1
        if stream_call_count == 1:
            assert messages[1]["content"] == [
                {"type": "thinking", "thinking": "I need a web fetch.", "signature": "sig_hist"},
                {"type": "text", "text": "Working..."},
            ]
            raise RuntimeError(
                "Anthropic API error: messages.1.content.0: `thinking` or "
                "`redacted_thinking` blocks in the latest assistant message cannot be modified."
            )

        assert messages[1]["content"] == [{"type": "text", "text": "Working..."}]
        for event_name, payload in [
            ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 7}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Recovered after thinking retry."}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}}),
            ("message_stop", {"type": "message_stop"}),
        ]:
            yield type("SSE", (), {"event": event_name, "data": json.dumps(payload)})()

    runtime._stream_anthropic_events = scripted_stream  # type: ignore[method-assign]

    await runtime.start()
    try:
        streamed_events = [event async for event in runtime.stream_task(task)]
    finally:
        await runtime.stop()

    retry_event = next(
        event
        for event in streamed_events
        if event["type"] == "task.progress" and event["status"] == "retrying"
    )
    assert "thinking-block replay" in retry_event["message"].lower()
    complete_event = next(event for event in streamed_events if event["type"] == "response.complete")
    assert complete_event["content"] == "Recovered after thinking retry."
    assert stream_call_count == 2


def test_orchestrator_stream_text_append_inserts_turn_boundary() -> None:
    assert (
        OrchestratorRuntime._append_stream_text(
            "Let me grab the artifact!",
            "Got it - firing Alpha now.",
        )
        == "Let me grab the artifact!\n\nGot it - firing Alpha now."
    )
    assert OrchestratorRuntime._append_stream_text("Hello", "world") == "Hello world"
    assert OrchestratorRuntime._append_stream_text("Hello ", "world") == "Hello world"


@pytest.mark.asyncio
async def test_orchestrator_runtime_emits_local_research_provenance_for_perplexity_and_firecrawl(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger_local_research.db",
    )
    runtime = OrchestratorRuntime(config, client=client)
    stream_call_count = 0

    async def scripted_stream(
        *,
        system_prompt: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        container_id: str | None = None,
        usage_context: dict[str, object] | None = None,
        model_override: str | None = None,
    ):
        del system_prompt, messages, tools, container_id, usage_context, model_override
        nonlocal stream_call_count
        stream_call_count += 1
        if stream_call_count == 1:
            events = [
                ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "tool_px_1", "name": "perplexity_research"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{\"query\":\"cursor composer 2 kimi k2.5\"}"}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tool_fc_1", "name": "firecrawl_scrape"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"url\":\"https://cursor.com/blog/composer\"}"}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 1}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 6}}),
                ("message_stop", {"type": "message_stop"}),
            ]
        else:
            events = [
                ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 12}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Done."}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}}),
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
        if tool_name == "perplexity_research":
            assert tool_input == {"query": "cursor composer 2 kimi k2.5"}
            return json.dumps(
                {
                    "answer": "Composer likely builds on Kimi K2.5.",
                    "citations": [
                        "https://cursor.com/blog/composer-2",
                        "https://news.ycombinator.com/item?id=44000000",
                    ],
                }
            )
        assert tool_name == "firecrawl_scrape"
        assert tool_input == {"url": "https://cursor.com/blog/composer"}
        return json.dumps({"url": "https://cursor.com/blog/composer", "available_formats": ["markdown"]})

    runtime._stream_anthropic_events = scripted_stream  # type: ignore[method-assign]

    await runtime.start()
    assert runtime._tool_executor is not None
    runtime._tool_executor.execute = fake_execute  # type: ignore[method-assign]
    try:
        streamed_events = [event async for event in runtime.stream_task(_signed_task("signing-secret"))]
    finally:
        await runtime.stop()

    complete_event = next(event for event in streamed_events if event["type"] == "response.complete")
    assert complete_event["research_provenance"] == {
        "paths": ["perplexity_research", "firecrawl"],
        "source_count": 2,
        "source_domains": ["cursor.com", "news.ycombinator.com"],
        "source_sample": [
            {
                "url": "https://cursor.com/blog/composer-2",
                "title": "cursor.com",
                "domain": "cursor.com",
            },
            {
                "url": "https://news.ycombinator.com/item?id=44000000",
                "title": "news.ycombinator.com",
                "domain": "news.ycombinator.com",
            },
        ],
    }


@pytest.mark.asyncio
async def test_orchestrator_runtime_emits_x_search_sources_as_research_provenance(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger_x_search_research.db",
    )
    runtime = OrchestratorRuntime(config, client=client)

    async def scripted_stream(**kwargs):
        del kwargs
        first_call = not getattr(scripted_stream, "_called", False)
        scripted_stream._called = True  # type: ignore[attr-defined]
        if first_call:
            events = [
                ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "tool_x_1", "name": "x_search"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{\"query\":\"Cursor Composer 2026\",\"max_posts\":30}"}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 4}}),
                ("message_stop", {"type": "message_stop"}),
            ]
        else:
            events = [
                ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 12}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Here are the latest tweets."}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}}),
                ("message_stop", {"type": "message_stop"}),
            ]
        for event_name, payload in events:
            yield type("SSE", (), {"event": event_name, "data": json.dumps(payload)})()

    async def fake_execute(tool_name: str, tool_input: dict[str, object], *, context=None) -> str:
        del context
        assert tool_name == "x_search"
        assert tool_input == {"query": "Cursor Composer 2026", "max_posts": 30}
        return json.dumps(
            {
                "summary": "Cursor Composer discourse is split between hype and licensing questions.",
                "notable_posts": [
                    {
                        "author_handle": "leerob",
                        "post_url": "https://x.com/leerob/status/123",
                        "excerpt": "Composer 2 is fast.",
                        "why_it_matters": "Cursor leadership reaction.",
                    }
                ],
                "citations": [],
            }
        )

    runtime._stream_anthropic_events = scripted_stream  # type: ignore[method-assign]

    await runtime.start()
    assert runtime._tool_executor is not None
    runtime._tool_executor.execute = fake_execute  # type: ignore[method-assign]
    try:
        streamed_events = [event async for event in runtime.stream_task(_signed_task("signing-secret"))]
    finally:
        await runtime.stop()

    complete_event = next(event for event in streamed_events if event["type"] == "response.complete")
    assert complete_event["sources"] == [
        {
            "url": "https://x.com/leerob/status/123",
            "title": "@leerob on X",
            "domain": "x.com",
        }
    ]
    assert complete_event["research_provenance"] == {
        "paths": ["x_search_specialist"],
        "source_count": 1,
        "source_domains": ["x.com"],
        "source_sample": [
            {
                "url": "https://x.com/leerob/status/123",
                "title": "@leerob on X",
                "domain": "x.com",
            }
        ],
    }
    assert complete_event["specialist_receipts"] == [
        {
            "tool_name": "x_search",
            "intent": "x.search",
            "agent_id": "cosmic/x-twitter-search-agent:1.0.0",
            "agent_label": "x twitter search agent",
            "activity": "used x.search via x twitter search agent",
            "source_count": 1,
            "source_domains": ["x.com"],
            "source_sample": [
                {
                    "url": "https://x.com/leerob/status/123",
                    "title": "@leerob on X",
                    "domain": "x.com",
                }
            ],
        }
    ]


@pytest.mark.asyncio
async def test_orchestrator_runtime_keeps_heartbeat_tool_artifacts_internal(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger_heartbeat_artifacts.db",
    )
    runtime = OrchestratorRuntime(config, client=client)

    async def scripted_stream(**kwargs):
        del kwargs
        first_call = not getattr(scripted_stream, "_called", False)
        scripted_stream._called = True  # type: ignore[attr-defined]
        if first_call:
            events = [
                ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "tool_x_heartbeat", "name": "x_search"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{\"query\":\"YC S26 invites\",\"max_posts\":8}"}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 4}}),
                ("message_stop", {"type": "message_stop"}),
            ]
        else:
            events = [
                ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 12}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "heartbeat_ok"}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}}),
                ("message_stop", {"type": "message_stop"}),
            ]
        for event_name, payload in events:
            yield type("SSE", (), {"event": event_name, "data": json.dumps(payload)})()

    async def fake_execute(tool_name: str, tool_input: dict[str, object], *, context=None) -> str:
        del context
        assert tool_name == "x_search"
        assert tool_input == {"query": "YC S26 invites", "max_posts": 8}
        return json.dumps(
            {
                "summary": "No new YC S26 invite wave found.",
                "artifacts": [
                    {
                        "artifact_id": "art_heartbeat_x_search_report",
                        "filename": "x_search_report.md",
                        "mime": "text/markdown",
                        "kind": "file",
                        "audience": "deliverable",
                        "path": "runs/artifacts/heartbeat/x_search_report.md",
                    }
                ],
            }
        )

    runtime._stream_anthropic_events = scripted_stream  # type: ignore[method-assign]
    task = _signed_task("signing-secret").model_copy(
        update={
            "task_id": "tsk_heartbeat_artifacts",
            "source": "heartbeat",
            "source_id": "default",
            "priority": "low",
            "input": {
                "query": "COSMIC HEARTBEAT",
                "request_id": "req_heartbeat_artifacts",
                "conversation_context": [],
            },
        }
    )
    task = task.model_copy(update={"signature": sign_task_envelope(task, "signing-secret")})

    await runtime.start()
    assert runtime._tool_executor is not None
    runtime._tool_executor.execute = fake_execute  # type: ignore[method-assign]
    try:
        streamed_events = [event async for event in runtime.stream_task(task)]
    finally:
        await runtime.stop()

    complete_event = next(event for event in streamed_events if event["type"] == "response.complete")
    assert complete_event["source"] == "heartbeat"
    assert complete_event["source_id"] == "default"
    assert complete_event["content"] == "heartbeat_ok"
    assert "produced_artifacts" not in complete_event


@pytest.mark.asyncio
async def test_orchestrator_runtime_inherits_x_search_provenance_from_delegate_to_agent(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger_delegate_x_research.db",
    )
    runtime = OrchestratorRuntime(config, client=client)

    async def scripted_stream(**kwargs):
        del kwargs
        first_call = not getattr(scripted_stream, "_called", False)
        scripted_stream._called = True  # type: ignore[attr-defined]
        if first_call:
            events = [
                ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "tool_delegate_1", "name": "delegate_to_agent"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{\"intent\":\"x.search\",\"input\":{\"query\":\"YC emergent vibecon April 2026\"}}"}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 4}}),
                ("message_stop", {"type": "message_stop"}),
            ]
        else:
            events = [
                ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 12}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Here are the latest X posts."}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}}),
                ("message_stop", {"type": "message_stop"}),
            ]
        for event_name, payload in events:
            yield type("SSE", (), {"event": event_name, "data": json.dumps(payload)})()

    async def fake_execute(tool_name: str, tool_input: dict[str, object], *, context=None) -> str:
        del context
        assert tool_name == "delegate_to_agent"
        assert tool_input["intent"] == "x.search"
        return json.dumps(
            {
                "summary": "VibeCon India is getting attention on X.",
                "notable_posts": [
                    {
                        "author_handle": "mukundjha",
                        "post_url": "https://x.com/mukundjha/status/123",
                        "excerpt": "Presenting Vibecon India.",
                        "why_it_matters": "Primary announcement.",
                    }
                ],
                "citations": [],
                "delegation": {
                    "intent": "x.search",
                    "agent_id": "cosmic/x-twitter-search-agent:1.0.0",
                },
            }
        )

    runtime._stream_anthropic_events = scripted_stream  # type: ignore[method-assign]

    await runtime.start()
    assert runtime._tool_executor is not None
    runtime._tool_executor.execute = fake_execute  # type: ignore[method-assign]
    try:
        streamed_events = [event async for event in runtime.stream_task(_signed_task("signing-secret"))]
    finally:
        await runtime.stop()

    complete_event = next(event for event in streamed_events if event["type"] == "response.complete")
    assert complete_event["sources"] == [
        {
            "url": "https://x.com/mukundjha/status/123",
            "title": "@mukundjha on X",
            "domain": "x.com",
        }
    ]
    assert complete_event["research_provenance"] == {
        "paths": ["x_search_specialist"],
        "source_count": 1,
        "source_domains": ["x.com"],
        "source_sample": [
            {
                "url": "https://x.com/mukundjha/status/123",
                "title": "@mukundjha on X",
                "domain": "x.com",
            }
        ],
    }
    assert complete_event["specialist_receipts"] == [
        {
            "tool_name": "delegate_to_agent",
            "intent": "x.search",
            "agent_id": "cosmic/x-twitter-search-agent:1.0.0",
            "agent_label": "x twitter search agent",
            "activity": "delegated x.search to x twitter search agent",
            "source_count": 1,
            "source_domains": ["x.com"],
            "source_sample": [
                {
                    "url": "https://x.com/mukundjha/status/123",
                    "title": "@mukundjha on X",
                    "domain": "x.com",
                }
            ],
        }
    ]


@pytest.mark.asyncio
async def test_orchestrator_runtime_summarizes_generic_specialist_tool_work(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger_specialist_summary.db",
    )
    runtime = OrchestratorRuntime(config, client=client)
    try:
        catalog_summary = runtime._summarize_local_tool_activity(
            "agent_catalog_search",
            {"query": "rendered page scrape"},
            json.dumps(
                {
                    "matches": [
                        {
                            "intent": "firecrawl.scrape",
                            "display_name": "Firecrawl Web Scrape Agent",
                        }
                    ],
                }
            ),
        )
        delegate_summary = runtime._summarize_local_tool_activity(
            "delegate_to_agent",
            {
                "intent": "firecrawl.extract",
                "agent_id": "cosmic/firecrawl-web-scrape-agent:1.0.0",
                "input": {"urls": ["https://example.com/a", "https://example.com/b"]},
            },
            json.dumps(
                {
                    "message": "Structured extraction completed for 2 pages.",
                    "delegation": {
                        "intent": "firecrawl.extract",
                        "agent_id": "cosmic/firecrawl-web-scrape-agent:1.0.0",
                    },
                }
            ),
        )
    finally:
        await runtime.stop()

    assert catalog_summary == "identified specialist intents: firecrawl.scrape via Firecrawl Web Scrape Agent"
    assert delegate_summary == (
        "delegated firecrawl.extract to firecrawl web scrape agent and structured extraction completed for 2 pages"
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
async def test_collect_specialist_receipt_captures_provider_model_and_fallback(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger_specialist_provider.db",
    )
    runtime = OrchestratorRuntime(config, client=client)
    try:
        receipts: list[dict[str, object]] = []
        runtime._collect_specialist_receipt(  # noqa: SLF001
            "delegate_to_agent",
            {
                "intent": "image.generate",
                "agent_id": "cosmic/image-generator-agent:1.0.0",
            },
            json.dumps(
                {
                    "message": "Generated 1 image via openai:gpt-image-1.5.",
                    "provider": "openai",
                    "model": "gpt-image-1.5",
                    "fallback_from": {
                        "provider": "xai",
                        "model": "grok-imagine-image-pro",
                    },
                    "delegation": {
                        "intent": "image.generate",
                        "agent_id": "cosmic/image-generator-agent:1.0.0",
                    },
                }
            ),
            specialist_receipts=receipts,
        )
    finally:
        await runtime.stop()

    assert receipts == [
        {
            "tool_name": "delegate_to_agent",
            "intent": "image.generate",
            "agent_id": "cosmic/image-generator-agent:1.0.0",
            "agent_label": "image generator agent",
            "activity": "delegated image.generate to image generator agent and generated 1 image via openai:gpt-image-1.5",
            "provider": "openai",
            "model": "gpt-image-1.5",
            "fallback_from": {
                "provider": "xai",
                "model": "grok-imagine-image-pro",
            },
        }
    ]


def test_build_agentic_system_prompt_includes_dynamic_specialist_shortlist() -> None:
    prompt = build_agentic_system_prompt(
        featured_specialists=[
            {
                "agent_id": "cosmic/docs-parser-agent:1.0.0",
                "display_name": "Docs Parser Agent",
                "agent_summary": "Parses documents and provides structured retrieval.",
                "common_intents": ["docs.parse_bundle", "docs.read_bundle"],
            }
        ]
    )

    assert "Current Specialist Shortlist" in prompt
    assert "small dynamically promoted subset" in prompt
    assert "not the full registry" in prompt
    assert "Docs Parser Agent" in prompt
    assert "docs.parse_bundle" in prompt
    assert "docs_browse" in prompt


def test_build_agentic_system_prompt_explains_trusted_inline_presentation_contract() -> None:
    prompt = build_agentic_system_prompt()

    assert "Never claim a specialist, tool, agent, or external system performed work unless that tool/specialist result is present in this turn." in prompt
    assert "trusted `_cosmic_ui` presentation contract" in prompt
    assert "do not duplicate fields listed in `covers` as Markdown" in prompt
    assert "Never claim an inline block will render unless the tool result includes this contract." in prompt
    assert "compose, draft, reply, redraft, revise, or rewrite" in prompt
    assert "do not stop at a Markdown preview" in prompt
    assert "`update_existing_draft=true`" in prompt


def test_build_agentic_system_prompt_gates_visual_response_policy() -> None:
    disabled_prompt = build_agentic_system_prompt()
    enabled_prompt = build_agentic_system_prompt(
        visual_response_enhancement_enabled=True
    )

    assert "## Visual Response Preference" not in disabled_prompt
    assert "## Visual Response Preference" in enabled_prompt
    assert "Do not mention the preference setting itself to the user." in enabled_prompt
    assert (
        "When this mode is enabled, proactively emit one inline visual when it would clearly anchor the answer"
        in enabled_prompt
    )


def test_get_prompt_asset_hashes_includes_visual_response_policy_asset() -> None:
    hashes = get_prompt_asset_hashes()

    assert "visual_response_policy.md" in hashes
    assert hashes["visual_response_policy.md"]


@pytest.mark.asyncio
async def test_search_agent_catalog_returns_usage_hints(tmp_path) -> None:
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger_usage_hints.db",
        agent_registry_db_path=tmp_path / "registry.db",
    )
    runtime = OrchestratorRuntime(config, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))))
    await runtime.start()
    try:
        runtime.registry_store.upsert_agent_card(
            {
                "agent_id": "cosmic/tabular-agent:1.0.0",
                "display_name": "Tabular Spreadsheet Agent",
                "description": "Specialist for spreadsheets.",
                "intents": [
                    {
                        "name": "tabular.reason_workbook",
                        "description": "Reason over a parsed workbook.",
                        "timeout_sec": 240,
                        "usage_hints": [
                            "Use reason_workbook for one delegated spreadsheet goal.",
                            "Prefer deterministic query tools when you already know the exact operation.",
                        ],
                    }
                ],
                "sla": {"max_concurrency": 1, "heartbeat_ttl_sec": 30, "max_task_duration_sec": 300},
            }
        )

        result = await runtime.search_agent_catalog(query="spreadsheet reasoning", require_healthy=False)
    finally:
        await runtime.stop()

    assert result["count"] == 1
    assert result["matches"][0]["intent"] == "tabular.reason_workbook"
    assert result["matches"][0]["usage_hints"] == [
        "Use reason_workbook for one delegated spreadsheet goal.",
        "Prefer deterministic query tools when you already know the exact operation.",
    ]


def test_orchestrator_build_messages_embeds_provider_fetchable_images(tmp_path) -> None:
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger_images.db",
    )
    runtime = OrchestratorRuntime(config, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))))
    task = _signed_task("signing-secret").model_copy(
        update={
            "input_artifacts": [
                {
                    "artifact_id": "art_img_001",
                    "kind": "image",
                    "mime": "image/png",
                    "filename": "chart.png",
                    "caption": "quarterly growth chart",
                    "provider_url": "https://gateway.example.test/artifacts/content/art_img_001?exp=123&sig=abc",
                    "provider_access": "signed_url",
                    "ingest_state": "staged",
                    "path": "runs/artifacts/req_ingest_req_test123/inputs/art_img_001/original/chart.png",
                }
            ]
        }
    )

    messages = runtime._build_messages(task)  # noqa: SLF001 - direct unit seam

    assert messages[-1]["role"] == "user"
    assert isinstance(messages[-1]["content"], list)
    assert messages[-1]["content"][0] == {
        "type": "image",
        "source": {
            "type": "url",
            "url": "https://gateway.example.test/artifacts/content/art_img_001?exp=123&sig=abc",
        },
    }
    assert messages[-1]["content"][1]["type"] == "text"
    assert "model_input=image_block" in messages[-1]["content"][1]["text"]


def test_orchestrator_build_messages_omits_images_past_cap(tmp_path) -> None:
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        anthropic_max_input_images=1,
        task_ledger_db_path=tmp_path / "task_ledger_images_cap.db",
    )
    runtime = OrchestratorRuntime(config, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))))
    task = _signed_task("signing-secret").model_copy(
        update={
            "input_artifacts": [
                {
                    "artifact_id": "art_img_001",
                    "kind": "image",
                    "mime": "image/png",
                    "filename": "chart.png",
                    "provider_url": "https://gateway.example.test/artifacts/content/art_img_001?exp=123&sig=abc",
                },
                {
                    "artifact_id": "art_img_002",
                    "kind": "image",
                    "mime": "image/jpeg",
                    "filename": "photo.jpg",
                    "provider_url": "https://gateway.example.test/artifacts/content/art_img_002?exp=123&sig=def",
                },
            ]
        }
    )

    messages = runtime._build_messages(task)  # noqa: SLF001 - direct unit seam

    assert messages[-1]["role"] == "user"
    assert isinstance(messages[-1]["content"], list)
    assert len(messages[-1]["content"]) == 2
    assert messages[-1]["content"][0]["type"] == "image"
    assert messages[-1]["content"][1]["type"] == "text"
    assert "model_input=omitted_limit" in messages[-1]["content"][1]["text"]
    assert "per-turn image cap is 1" in messages[-1]["content"][1]["text"]


@pytest.mark.asyncio
async def test_orchestrator_attaches_initial_input_artifacts_as_container_uploads(tmp_path) -> None:
    upload_calls: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL("https://api.anthropic.com/v1/files"):
            upload_calls.append({"headers": dict(request.headers)})
            return httpx.Response(
                200,
                json={
                    "id": "file_input_001",
                    "filename": "reference.png",
                    "mime_type": "image/png",
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = OrchestratorConfig(
        artifacts_root=tmp_path / "artifacts",
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger_input_uploads.db",
    )
    runtime = OrchestratorRuntime(config, client=client)
    try:
        artifact_path = config.artifacts_root / "tsk_prev" / "image_generator_agent" / "reference.png"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_bytes(_PNG_BYTES)
        task = _signed_task("signing-secret").model_copy(
            update={
                "input_artifacts": [
                    {
                        "artifact_id": "art_img_ref",
                        "kind": "image",
                        "mime": "image/png",
                        "filename": "reference.png",
                        "path": "runs/artifacts/tsk_prev/image_generator_agent/reference.png",
                    }
                ]
            }
        )

        messages = runtime._build_messages(task)  # noqa: SLF001
        messages = await runtime._attach_initial_input_artifact_blocks(messages, task.input_artifacts)  # noqa: SLF001
    finally:
        await runtime.stop()

    assert upload_calls
    assert messages[-1]["role"] == "user"
    assert isinstance(messages[-1]["content"], list)
    assert any(
        isinstance(block, dict) and block.get("type") == "container_upload" and block.get("file_id") == "file_input_001"
        for block in messages[-1]["content"]
    )


@pytest.mark.asyncio
async def test_orchestrator_build_tool_result_followup_blocks_attach_image_and_code_inputs(tmp_path) -> None:
    upload_calls: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL("https://api.anthropic.com/v1/files"):
            upload_calls.append({"headers": dict(request.headers)})
            return httpx.Response(
                200,
                json={
                    "id": "file_input_002",
                    "filename": "apple_product_launch_style.png",
                    "mime_type": "image/png",
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = OrchestratorConfig(
        artifacts_root=tmp_path / "artifacts",
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        task_ledger_db_path=tmp_path / "task_ledger_tool_followups.db",
    )
    runtime = OrchestratorRuntime(config, client=client)
    try:
        artifact_path = config.artifacts_root / "tsk_prev" / "image_generator_agent" / "apple_product_launch_style.png"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_bytes(_PNG_BYTES)
        result_str = json.dumps(
            {
                "found": True,
                "artifacts": [
                    {
                        "artifact_id": "art_launch_sphere",
                        "mime": "image/png",
                        "filename": "apple_product_launch_style.png",
                        "path": "runs/artifacts/tsk_prev/image_generator_agent/apple_product_launch_style.png",
                    }
                ],
            }
        )

        blocks = await runtime._build_tool_result_followup_blocks([result_str])  # noqa: SLF001
    finally:
        await runtime.stop()

    assert upload_calls
    assert blocks[0]["type"] == "text"
    assert any(isinstance(block, dict) and block.get("type") == "image" for block in blocks)
    assert any(
        isinstance(block, dict) and block.get("type") == "container_upload" and block.get("file_id") == "file_input_002"
        for block in blocks
    )


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


@pytest.mark.asyncio
async def test_orchestrator_runtime_retries_transient_overload_before_response_text(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        anthropic_overload_retry_attempts=1,
        anthropic_overload_initial_backoff_sec=0.01,
        anthropic_overload_max_backoff_sec=0.01,
        task_ledger_db_path=tmp_path / "task_ledger_overload_retry.db",
    )
    runtime = OrchestratorRuntime(config, client=client)
    task = _signed_task("signing-secret")
    stream_call_count = 0

    async def scripted_stream(**kwargs):
        nonlocal stream_call_count
        del kwargs
        stream_call_count += 1
        if stream_call_count == 1:
            events = [
                _fake_sse("message", {"type": "message_start", "message": {"usage": {"input_tokens": 12}}}),
                _fake_sse("message", {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}}),
                _fake_sse(
                    "message",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "thinking_delta", "thinking": "Checking capacity..."},
                    },
                ),
                _fake_sse("message", {"type": "error", "error": {"message": "Overloaded"}}),
            ]
        else:
            events = [
                _fake_sse("message", {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
                _fake_sse("message", {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
                _fake_sse(
                    "message",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "Your calendar is clear today."},
                    },
                ),
                _fake_sse("message", {"type": "content_block_stop", "index": 0}),
                _fake_sse(
                    "message",
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 8}},
                ),
                _fake_sse("message", {"type": "message_stop"}),
            ]
        for item in events:
            yield item

    runtime._stream_anthropic_events = scripted_stream  # type: ignore[method-assign]

    await runtime.start()
    try:
        streamed_events = [event async for event in runtime.stream_task(task)]
    finally:
        await runtime.stop()

    assert stream_call_count == 2
    retry_events = [
        event for event in streamed_events if event["type"] == "task.progress" and event.get("status") == "retrying"
    ]
    assert retry_events == [
        {
            "task_id": "tsk_test123",
            "request_id": "req_test123",
            "session_id": "sess_20260307",
            "channel": "desktop:desk_test",
            "type": "task.progress",
            "status": "retrying",
            "iteration": 1,
            "message": "Opus hit temporary capacity. Retrying automatically...",
        }
    ]
    complete_event = next(event for event in streamed_events if event["type"] == "response.complete")
    assert complete_event["content"] == "Your calendar is clear today."
    assert streamed_events[-1]["type"] == "task.completed"


@pytest.mark.asyncio
async def test_orchestrator_runtime_surfaces_friendly_overload_failure_after_retries_exhaust(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        anthropic_overload_retry_attempts=1,
        anthropic_overload_initial_backoff_sec=0.01,
        anthropic_overload_max_backoff_sec=0.01,
        task_ledger_db_path=tmp_path / "task_ledger_overload_fail.db",
    )
    runtime = OrchestratorRuntime(config, client=client)
    task = _signed_task("signing-secret")
    stream_call_count = 0

    async def scripted_stream(**kwargs):
        nonlocal stream_call_count
        del kwargs
        stream_call_count += 1
        if False:
            yield None
        raise RuntimeError("Overloaded")

    runtime._stream_anthropic_events = scripted_stream  # type: ignore[method-assign]

    await runtime.start()
    try:
        streamed_events = [event async for event in runtime.stream_task(task)]
    finally:
        await runtime.stop()

    assert stream_call_count == 2
    failed_event = next(event for event in streamed_events if event["type"] == "task.failed")
    assert failed_event["error"] == {
        "code": "OPUS_TEMPORARILY_OVERLOADED",
        "message": "Opus is temporarily overloaded right now. Please try again in a moment.",
        "retryable": True,
    }


@pytest.mark.asyncio
async def test_orchestrator_runtime_can_retry_with_fallback_model_after_overload(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        anthropic_overload_retry_attempts=0,
        anthropic_overload_initial_backoff_sec=0.01,
        anthropic_overload_max_backoff_sec=0.01,
        anthropic_overload_fallback_model="claude-haiku-4-5",
        task_ledger_db_path=tmp_path / "task_ledger_overload_fallback.db",
    )
    runtime = OrchestratorRuntime(config, client=client)
    task = _signed_task("signing-secret")
    model_overrides: list[str | None] = []

    async def scripted_stream(**kwargs):
        model_overrides.append(kwargs.get("model_override"))
        if len(model_overrides) == 1:
            if False:
                yield None
            raise RuntimeError("Overloaded")
        yield _fake_sse("message", {"type": "message_start", "message": {"usage": {"input_tokens": 10}}})
        yield _fake_sse("message", {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}})
        yield _fake_sse(
            "message",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Fallback model completed the turn."},
            },
        )
        yield _fake_sse("message", {"type": "content_block_stop", "index": 0})
        yield _fake_sse(
            "message",
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 6}},
        )
        yield _fake_sse("message", {"type": "message_stop"})

    runtime._stream_anthropic_events = scripted_stream  # type: ignore[method-assign]

    await runtime.start()
    try:
        streamed_events = [event async for event in runtime.stream_task(task)]
    finally:
        await runtime.stop()

    assert model_overrides == [None, "claude-haiku-4-5"]
    retry_event = next(
        event for event in streamed_events if event["type"] == "task.progress" and event.get("status") == "retrying"
    )
    assert retry_event["message"] == "Opus hit temporary capacity. Retrying with a standby model..."
    complete_event = next(event for event in streamed_events if event["type"] == "response.complete")
    assert complete_event["content"] == "Fallback model completed the turn."


@pytest.mark.asyncio
async def test_collect_specialist_receipt_captures_calendar_event(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    runtime = OrchestratorRuntime(
        OrchestratorConfig(
            internal_token="internal-token",
            signing_secret="signing-secret",
            anthropic_api_key="anthropic-key",
            anthropic_model="claude-opus-4-6",
            task_ledger_db_path=tmp_path / "task_ledger_calendar_receipt.db",
        ),
        client=client,
    )
    try:
        receipts: list[dict[str, object]] = []
        runtime._collect_specialist_receipt(  # noqa: SLF001
            "delegate_to_agent",
            {"intent": "calendar.create_event", "agent_id": "cosmic/calendar-agent:1.0.0"},
            json.dumps(
                {
                    "delegation": {
                        "intent": "calendar.create_event",
                        "agent_id": "cosmic/calendar-agent:1.0.0",
                    },
                    "event": {
                        "event_id": "evt_123",
                        "calendar_id": "owner@example.com",
                        "summary": "Design review",
                        "start": "2026-06-06T10:00:00-05:00",
                        "end": "2026-06-06T10:30:00-05:00",
                        "html_link": "https://calendar.google.com/event?eid=evt_123",
                    },
                }
            ),
            specialist_receipts=receipts,
        )
    finally:
        await runtime.stop()

    assert receipts[0]["calendar_event"] == {
        "operation": "created",
        "event_id": "evt_123",
        "calendar_id": "owner@example.com",
        "summary": "Design review",
        "start": "2026-06-06T10:00:00-05:00",
        "end": "2026-06-06T10:30:00-05:00",
        "html_link": "https://calendar.google.com/event?eid=evt_123",
        "is_all_day": False,
    }


@pytest.mark.asyncio
async def test_collect_specialist_receipt_captures_direct_alpha_project(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    runtime = OrchestratorRuntime(
        OrchestratorConfig(
            internal_token="internal-token",
            signing_secret="signing-secret",
            anthropic_api_key="anthropic-key",
            anthropic_model="claude-opus-4-6",
            task_ledger_db_path=tmp_path / "task_ledger_alpha_receipt.db",
        ),
        client=client,
    )
    try:
        receipts: list[dict[str, object]] = []
        runtime._collect_specialist_receipt(  # noqa: SLF001
            "delegate_to_agent",
            {"intent": "alpha.execute", "agent_id": "cosmic/alpha-agent:1.0.0"},
            json.dumps(
                {
                    "delegation": {
                        "intent": "alpha.execute",
                        "agent_id": "cosmic/alpha-agent:1.0.0",
                    },
                    "project": {
                        "project_id": "alpha_proj_crm_123",
                        "status": "live",
                        "repo_url": "https://github.com/example/crm",
                        "deployment_url": "https://crm.example.test",
                        "last_task_id": "tsk_alpha_123",
                    },
                }
            ),
            specialist_receipts=receipts,
        )
    finally:
        await runtime.stop()

    assert receipts[0]["alpha_project"] == {
        "alpha_project_id": "alpha_proj_crm_123",
        "status": "live",
        "repo_url": "https://github.com/example/crm",
        "deployment_url": "https://crm.example.test",
        "last_task_id": "tsk_alpha_123",
    }


@pytest.mark.asyncio
async def test_collect_specialist_receipt_captures_real_gmail_draft_reply_shape(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    runtime = OrchestratorRuntime(
        OrchestratorConfig(
            internal_token="internal-token",
            signing_secret="signing-secret",
            anthropic_api_key="anthropic-key",
            anthropic_model="claude-opus-4-6",
            task_ledger_db_path=tmp_path / "task_ledger_gmail_receipt.db",
        ),
        client=client,
    )
    try:
        receipts: list[dict[str, object]] = []
        runtime._collect_specialist_receipt(  # noqa: SLF001
            "delegate_to_agent",
            {"intent": "gmail.draft_reply", "agent_id": "cosmic/gmail-agent:1.0.0"},
            json.dumps(
                {
                    "status": "draft_created",
                    "account": {
                        "account_id": "acct_123",
                        "account_email": "owner@example.com",
                        "account_label": "Primary",
                    },
                    "draft_id": "draft_123",
                    "message": {"id": "msg_123", "threadId": "thread_123"},
                    "approval_required": True,
                    "delivery_status": "draft_created_pending_user_approval",
                    "draft": {
                        "to": ["recipient@example.com"],
                        "subject": "Quick follow-up",
                        "body": "Hi, following up.",
                    },
                    "delegation": {
                        "intent": "gmail.draft_reply",
                        "agent_id": "cosmic/gmail-agent:1.0.0",
                    },
                }
            ),
            specialist_receipts=receipts,
        )
    finally:
        await runtime.stop()

    assert receipts[0]["gmail_approval"] == {
        "account_id": "acct_123",
        "account_email": "owner@example.com",
        "account_label": "Primary",
        "draft_id": "draft_123",
        "message_id": "msg_123",
        "thread_id": "thread_123",
        "subject": "Quick follow-up",
        "body_text": "Hi, following up.",
        "body_preview": "Hi, following up.",
        "to": ["recipient@example.com"],
    }
