from __future__ import annotations

import json

import httpx
import pytest

from orchestrator.tools.executor import ToolExecutionContext, ToolExecutor


@pytest.mark.asyncio
async def test_tool_executor_memory_search_uses_gateway_active_search_and_preserves_payload() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("http://gateway/internal/memory/active-search")
        assert request.headers["X-Internal-Token"] == "internal-token"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload == {
            "query": "memory integration",
            "max_results": 1,
            "token_budget": 4096,
            "kinds": ["task_summary"],
        }
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "memory_id": "mem_task_1",
                        "kind": "task_summary",
                        "title": "Memory integration work",
                        "content": "X" * 2400,
                        "path": "memory/tasks/tsk_1.md",
                        "metadata": {"session_id": "sess_1", "task_id": "tsk_1"},
                        "score": 0.98,
                    }
                ],
                "entities": [{"name": "COSMIC", "kind": "project"}],
                "relations": [{"from": "COSMIC", "to": "Gateway", "type": "uses"}],
                "episodes": [{"episode_id": "ep_1"}],
                "search_plan": [{"step": "semantic_lookup"}],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    executor = ToolExecutor(
        gateway_url="http://gateway",
        gateway_internal_token="internal-token",
        client=client,
    )
    try:
        raw_result = await executor.execute(
            "memory_search",
            {
                "query": "memory integration",
                "max_results": 1,
                "token_budget": 4096,
                "kinds": ["task_summary"],
            },
        )
    finally:
        await client.aclose()

    result = json.loads(raw_result)
    assert result["items"][0]["memory_id"] == "mem_task_1"
    assert result["results"][0]["path"] == "memory/tasks/tsk_1.md"
    assert len(result["items"][0]["content"]) == 2400
    assert result["entities"][0]["name"] == "COSMIC"
    assert result["search_plan"][0]["step"] == "semantic_lookup"


@pytest.mark.asyncio
async def test_tool_executor_memory_write_uses_gateway_and_enriches_contextual_metadata() -> None:
    seen_payload: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("http://gateway/internal/memory/write")
        seen_payload.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"memory_id": "mem_saved_1"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    executor = ToolExecutor(
        gateway_url="http://gateway",
        gateway_internal_token="internal-token",
        client=client,
    )
    context = ToolExecutionContext(
        task_id="tsk_current",
        request_id="req_current",
        session_id="sess_current",
        channel="desktop:desk_a",
        source="user",
        source_id="desktop",
    )
    try:
        raw_result = await executor.execute(
            "memory_write",
            {
                "content": "User prefers concise architectural explanations.",
                "kind": "preference",
                "tags": ["preference", "style"],
            },
            context=context,
        )
    finally:
        await client.aclose()

    result = json.loads(raw_result)
    assert result["saved"] is True
    assert result["id"] == "mem_saved_1"
    assert seen_payload["title"] == "User prefers concise architectural explanations."
    assert seen_payload["tags"] == ["preference", "style"]
    assert seen_payload["metadata"]["session_id"] == "sess_current"
    assert seen_payload["metadata"]["task_id"] == "tsk_current"
    assert seen_payload["provenance"]["created_by"] == "cosmic/orchestrator:1.0.0"
    assert seen_payload["provenance"]["request_id"] == "req_current"


@pytest.mark.asyncio
async def test_tool_executor_session_revisit_defaults_to_current_context_and_task_notebook_handles_missing() -> None:
    revisit_payloads: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL("http://gateway/internal/session/revisit"):
            payload = json.loads(request.content.decode("utf-8"))
            revisit_payloads.append(payload)
            return httpx.Response(
                200,
                json={
                    "session": {"session_id": payload["session_id"]},
                    "turn_ledger": [],
                    "raw_history": [],
                    "task_notebook": {"task_id": payload["task_id"]},
                    "turn": {"request_id": payload["request_id"]},
                },
            )
        if request.url == httpx.URL("http://gateway/internal/session/task-notebook/tsk_current"):
            return httpx.Response(404, json={"detail": "Task notebook not found"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    executor = ToolExecutor(
        gateway_url="http://gateway",
        gateway_internal_token="internal-token",
        client=client,
    )
    context = ToolExecutionContext(
        task_id="tsk_current",
        request_id="req_current",
        session_id="sess_current",
    )
    try:
        revisit_raw = await executor.execute("session_revisit", {}, context=context)
        notebook_raw = await executor.execute("task_notebook", {}, context=context)
    finally:
        await client.aclose()

    revisit_result = json.loads(revisit_raw)
    notebook_result = json.loads(notebook_raw)
    assert revisit_payloads == [
        {
            "session_id": "sess_current",
            "turn_limit": 8,
            "raw_history_limit": 12,
            "task_id": "tsk_current",
            "request_id": "req_current",
        }
    ]
    assert revisit_result["session"]["session_id"] == "sess_current"
    assert revisit_result["task_notebook"]["task_id"] == "tsk_current"
    assert notebook_result == {
        "found": False,
        "task_id": "tsk_current",
        "message": "Task notebook not found.",
    }
