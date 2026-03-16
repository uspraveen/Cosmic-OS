from __future__ import annotations

import json

import httpx
import pytest

from orchestrator.tools.executor import ToolExecutionContext, ToolExecutor
from shared import AgentError, AgentResult, TaskEnvelope, TaskInProgress, sign_task_envelope, utcnow


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
            "seed_memory_ids": ["mem_anchor_1"],
            "seed_entities": ["COSMIC"],
            "max_hops": 3,
            "include_diagnostics": True,
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
                "diagnostics": {"flags": {"graph_assist_used": True}},
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
                "seed_memory_ids": ["mem_anchor_1"],
                "seed_entities": ["COSMIC"],
                "max_hops": 3,
                "include_diagnostics": True,
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
    assert result["diagnostics"]["flags"]["graph_assist_used"] is True


@pytest.mark.asyncio
async def test_tool_executor_memory_fetch_reads_full_memory_record_via_gateway() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("http://gateway/internal/memory/memories/mem_task_1")
        assert request.headers["X-Internal-Token"] == "internal-token"
        return httpx.Response(
            200,
            json={
                "memory_id": "mem_task_1",
                "kind": "task_summary",
                "title": "Memory integration work",
                "content": "Full canonical memory body",
                "tags": ["architecture"],
                "metadata": {"task_id": "tsk_1"},
                "provenance": {"source_kind": "gateway"},
                "status": "active",
                "version": 2,
                "created_at": "2026-03-15T00:00:00Z",
                "updated_at": "2026-03-15T00:00:00Z",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    executor = ToolExecutor(
        gateway_url="http://gateway",
        gateway_internal_token="internal-token",
        client=client,
    )
    try:
        raw_result = await executor.execute("memory_fetch", {"memory_id": "mem_task_1"})
    finally:
        await client.aclose()

    result = json.loads(raw_result)
    assert result["found"] is True
    assert result["memory_id"] == "mem_task_1"
    assert result["content"] == "Full canonical memory body"
    assert result["metadata"]["task_id"] == "tsk_1"
    assert result["record"]["version"] == 2


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
    assert result["kind"] == "user_data"
    assert result["original_kind"] == "preference"
    assert seen_payload["kind"] == "user_data"
    assert seen_payload["title"] == "User prefers concise architectural explanations."
    assert seen_payload["tags"] == ["preference", "style"]
    assert seen_payload["metadata"]["session_id"] == "sess_current"
    assert seen_payload["metadata"]["task_id"] == "tsk_current"
    assert seen_payload["provenance"]["created_by"] == "cosmic/orchestrator:1.0.0"
    assert seen_payload["provenance"]["request_id"] == "req_current"


@pytest.mark.asyncio
async def test_tool_executor_memory_write_core_fact_uses_gateway_and_enriches_contextual_metadata() -> None:
    seen_payload: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("http://gateway/internal/memory/core-facts")
        assert request.headers["X-Internal-Token"] == "internal-token"
        seen_payload.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"memory_id": "mem_core_1"})

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
            "memory_write_core_fact",
            {
                "fact": "User prefers concise architectural explanations.",
                "canonical_key": "preferences.response_style",
                "tags": ["preference", "style"],
                "always_include": "false",
            },
            context=context,
        )
    finally:
        await client.aclose()

    result = json.loads(raw_result)
    assert result["saved"] is True
    assert result["id"] == "mem_core_1"
    assert result["kind"] == "core_fact"
    assert result["canonical_key"] == "preferences.response_style"
    assert seen_payload["canonical_key"] == "preferences.response_style"
    assert seen_payload["always_include"] is False
    assert seen_payload["metadata"]["session_id"] == "sess_current"
    assert seen_payload["metadata"]["task_id"] == "tsk_current"
    assert seen_payload["provenance"]["created_by"] == "cosmic/orchestrator:1.0.0"
    assert seen_payload["provenance"]["request_id"] == "req_current"


@pytest.mark.asyncio
async def test_tool_executor_create_reminder_uses_internal_scheduler_route_and_passes_gateway_resolution_inputs() -> None:
    seen_payload: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("http://gateway/internal/scheduler/crons")
        assert request.headers["X-Internal-Token"] == "internal-token"
        seen_payload.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "cron_id": "cron_abc123",
                "label": "Morning YC check",
                "cron_expression": "0 6 * * *",
                "timezone": "America/Chicago",
                "next_fire_at": "2026-03-16T11:00:00Z",
                "next_fire_local": "Monday, March 16, 2026 at 06:00 AM CDT",
                "delivery_target": "whatsapp",
                "delivery_channel": "whatsapp:+12153079021",
                "resolved_delivery_channel": "whatsapp:+12153079021",
                "context_summary": "Diff the saved YC company baseline and report additions or no change.",
            },
        )

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
    )
    try:
        raw_result = await executor.execute(
            "create_reminder",
            {
                "label": "Morning YC check",
                "cron_expression": "0 6 * * *",
                "prompt": "Check for new YC companies and report the diff.",
                "delivery_target": "whatsapp",
                "context_summary": "Diff the saved YC company baseline and report additions or no change.",
                "one_shot": True,
            },
            context=context,
        )
    finally:
        await client.aclose()

    result = json.loads(raw_result)
    assert seen_payload == {
        "label": "Morning YC check",
        "cron_expression": "0 6 * * *",
        "prompt": "Check for new YC companies and report the diff.",
        "delivery_target": "whatsapp",
        "context_summary": "Diff the saved YC company baseline and report additions or no change.",
        "one_shot": True,
        "source": "orchestrator",
        "request_id": "req_current",
        "session_id": "sess_current",
        "channel": "desktop:desk_a",
    }
    assert result["created"] is True
    assert result["cron_id"] == "cron_abc123"
    assert result["timezone"] == "America/Chicago"
    assert result["next_fire_at"] == "2026-03-16T11:00:00Z"
    assert result["delivery_target"] == "whatsapp"
    assert result["delivery_channel"] == "whatsapp:+12153079021"
    assert result["resolved_delivery_channel"] == "whatsapp:+12153079021"
    assert result["context_summary"] == "Diff the saved YC company baseline and report additions or no change."


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


def _parent_task() -> TaskEnvelope:
    task = TaskEnvelope(
        task_id="tsk_parent",
        task_list_id="sess_parent",
        parent_task_id=None,
        session_id="sess_parent",
        sender="cosmic/gateway:1.0.0",
        recipient="cosmic/orchestrator:1.0.0",
        intent="orchestrator.process",
        input={"query": "research this page", "request_id": "req_parent"},
        input_artifacts=[],
        idempotency_key="idem_parent",
        priority="normal",
        signature="",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:test",
    )
    return task.model_copy(update={"signature": sign_task_envelope(task, "signing-secret")})


@pytest.mark.asyncio
async def test_tool_executor_agent_catalog_search_returns_matches_from_callback() -> None:
    observed: dict[str, object] = {}

    async def searcher(**kwargs):
        observed.update(kwargs)
        return {
            "query": "rendered page scrape",
            "matches": [
                {
                    "intent": "firecrawl.scrape",
                    "agent_id": "cosmic/firecrawl-web-scrape-agent:1.0.0",
                    "display_name": "Firecrawl Web Scrape Agent",
                    "healthy": True,
                    "input_schema_summary": {
                        "required": ["url"],
                        "properties": [
                            {"name": "url", "type": "string"},
                            {"name": "formats", "type": "array<string>"},
                        ],
                    },
                }
            ],
            "count": 1,
            "message": "Found 1 matching specialist intents.",
        }

    executor = ToolExecutor(agent_catalog_searcher=searcher)
    raw_result = await executor.execute(
        "agent_catalog_search",
        {
            "query": "rendered page scrape",
            "limit": 2,
        },
    )

    result = json.loads(raw_result)
    assert observed == {
        "query": "rendered page scrape",
        "limit": 2,
        "require_healthy": True,
    }
    assert result["matches"][0]["intent"] == "firecrawl.scrape"
    assert result["matches"][0]["input_schema_summary"]["required"] == ["url"]


@pytest.mark.asyncio
async def test_tool_executor_delegate_to_agent_dispatches_specialist_agent_and_returns_output() -> None:
    observed: dict[str, object] = {}

    async def dispatcher(**kwargs):
        observed.update(kwargs)
        return AgentResult(
            status="completed",
            output={
                "response": "Scraped https://example.com/post and captured markdown.",
                "message": "Scraped https://example.com/post and captured markdown.",
                "url": "https://example.com/post",
                "title": "Example",
                "available_formats": ["markdown"],
                "metadata": {"title": "Example"},
                "data": {"markdown_excerpt": "# Example"},
                "artifacts": [{"artifact_id": "art_1", "path": "runs/artifacts/tsk_child/page.md", "mime": "text/markdown"}],
            },
            artifacts=[],
        )

    executor = ToolExecutor(agent_dispatcher=dispatcher)
    context = ToolExecutionContext(
        task_id="tsk_parent",
        request_id="req_parent",
        session_id="sess_parent",
        channel="desktop:test",
        source="user",
        source_id="desktop",
        parent_task=_parent_task(),
    )
    raw_result = await executor.execute(
        "delegate_to_agent",
        {
            "intent": "firecrawl.scrape",
            "agent_id": "cosmic/firecrawl-web-scrape-agent:1.0.0",
            "input": {
                "url": "https://example.com/post",
                "formats": ["markdown"],
                "wait_for_ms": 1000,
            },
        },
        context=context,
    )

    result = json.loads(raw_result)
    assert observed["intent"] == "firecrawl.scrape"
    assert observed["agent_id"] == "cosmic/firecrawl-web-scrape-agent:1.0.0"
    assert observed["parent_task"].task_id == "tsk_parent"
    assert observed["input_payload"] == {
        "url": "https://example.com/post",
        "formats": ["markdown"],
        "wait_for_ms": 1000,
    }
    assert result["url"] == "https://example.com/post"
    assert result["available_formats"] == ["markdown"]
    assert result["delegation"] == {
        "intent": "firecrawl.scrape",
        "agent_id": "cosmic/firecrawl-web-scrape-agent:1.0.0",
    }


@pytest.mark.asyncio
async def test_tool_executor_delegate_to_agent_returns_agent_failure_payload() -> None:
    async def dispatcher(**kwargs):
        del kwargs
        return AgentResult(
            status="failed",
            output={},
            artifacts=[],
            error=AgentError(
                code="RATE_LIMITED",
                retryable=True,
                message="Firecrawl rate limit exceeded.",
                next_action="retry",
            ),
        )

    executor = ToolExecutor(agent_dispatcher=dispatcher)
    context = ToolExecutionContext(parent_task=_parent_task())
    raw_result = await executor.execute(
        "delegate_to_agent",
        {
            "intent": "firecrawl.extract",
            "input": {
                "urls": ["https://example.com/a"],
                "prompt": "Extract the company names.",
            },
        },
        context=context,
    )

    result = json.loads(raw_result)
    assert result == {
        "error": True,
        "code": "RATE_LIMITED",
        "retryable": True,
        "next_action": "retry",
        "message": "Firecrawl rate limit exceeded.",
        "delegation": {"intent": "firecrawl.extract", "agent_id": None},
    }


@pytest.mark.asyncio
async def test_tool_executor_delegate_to_agent_handles_in_progress_result() -> None:
    observed: dict[str, object] = {}

    async def dispatcher(**kwargs):
        observed.update(kwargs)
        return TaskInProgress(
            task_id="tsk_child_firecrawl",
            idempotency_key="idem_child",
            executing_since=utcnow(),
            check_after_sec=8,
        )

    executor = ToolExecutor(agent_dispatcher=dispatcher)
    context = ToolExecutionContext(parent_task=_parent_task(), session_id="sess_parent")
    raw_result = await executor.execute(
        "delegate_to_agent",
        {
            "intent": "firecrawl.recall_session",
            "input": {"session_id": "sess_parent", "limit": 5},
        },
        context=context,
    )

    result = json.loads(raw_result)
    assert observed["input_payload"] == {"session_id": "sess_parent", "limit": 5}
    assert result == {
        "error": True,
        "in_progress": True,
        "task_id": "tsk_child_firecrawl",
        "idempotency_key": "idem_child",
        "check_after_sec": 8,
        "message": "firecrawl.recall_session is still running in the specialist agent.",
        "delegation": {"intent": "firecrawl.recall_session", "agent_id": None},
    }
