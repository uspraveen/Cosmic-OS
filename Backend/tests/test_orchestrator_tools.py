from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from orchestrator.tools.executor import ToolExecutionContext, ToolExecutor
from orchestrator.tools.registry import build_tool_prompt_catalog, get_local_tool_definitions
from shared import AgentError, AgentResult, TaskEnvelope, TaskInProgress, sign_task_envelope, utcnow


def test_cosmic_code_execution_is_registered_as_local_tool() -> None:
    tool_names = {tool.get("name") for tool in get_local_tool_definitions()}
    assert "cosmic_code_execution" in tool_names
    assert "heartbeat_notes" in tool_names


def test_cosmic_code_execution_warns_maps_must_use_map_specialist() -> None:
    tool = next(tool for tool in get_local_tool_definitions() if tool.get("name") == "cosmic_code_execution")
    description = str(tool.get("description") or "")
    assert "map.render" in description
    assert "inline COSMIC map artifacts" in description
    assert "Folium" in description

    catalog = build_tool_prompt_catalog()
    assert "Use map.render, not this sandbox, for maps/routes/place visuals" in catalog


@pytest.mark.asyncio
async def test_tool_executor_cosmic_code_execution_runs_python_and_captures_stdout() -> None:
    root = Path.cwd() / ".pytest-local-code-sandbox" / uuid4().hex
    try:
        executor = ToolExecutor(artifacts_root=root / "artifacts")
        raw_result = await executor.execute(
            "cosmic_code_execution",
            {
                "description": "calculate a simple value",
                "code": "print(6 * 7)",
            },
            context=ToolExecutionContext(task_id="tsk_calc", session_id="sess_calc"),
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)

    result = json.loads(raw_result)
    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    assert "42" in result["stdout"]
    assert result["artifacts"] == []


@pytest.mark.asyncio
async def test_tool_executor_cosmic_code_execution_collects_output_artifacts() -> None:
    root = Path.cwd() / ".pytest-local-code-sandbox" / uuid4().hex
    artifacts_root = root / "artifacts"
    try:
        executor = ToolExecutor(artifacts_root=artifacts_root)
        raw_result = await executor.execute(
            "cosmic_code_execution",
            {
                "description": "write a deliverable text file",
                "code": (
                    "from pathlib import Path\n"
                    "Path('outputs/result.txt').write_text('hello from sandbox', encoding='utf-8')\n"
                    "print('wrote file')\n"
                ),
            },
            context=ToolExecutionContext(task_id="tsk_file", session_id="sess_file"),
        )
        result = json.loads(raw_result)
        assert result["status"] == "completed"
        assert result["artifact_count"] == 1
        artifact = result["artifacts"][0]
        assert artifact["filename"] == "result.txt"
        assert artifact["audience"] == "deliverable"
        assert artifact["path"].startswith("runs/artifacts/")
        stored_path = artifacts_root / artifact["path"].removeprefix("runs/artifacts/")
        assert stored_path.read_text(encoding="utf-8") == "hello from sandbox"
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_tool_executor_cosmic_code_execution_blocks_path_escape() -> None:
    root = Path.cwd() / ".pytest-local-code-sandbox" / uuid4().hex
    try:
        executor = ToolExecutor(artifacts_root=root / "artifacts")
        raw_result = await executor.execute(
            "cosmic_code_execution",
            {"code": "open('../escape.txt', 'w').write('bad')"},
            context=ToolExecutionContext(task_id="tsk_escape", session_id="sess_escape"),
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)

    result = json.loads(raw_result)
    assert result["status"] == "failed"
    assert result["exit_code"] != 0
    assert "Path escapes COSMIC code sandbox" in result["stderr"]
    assert not (root / "artifacts" / "tsk_escape" / "orchestrator" / "escape.txt").exists()


@pytest.mark.asyncio
async def test_tool_executor_artifact_read_loads_supporting_scrape_file() -> None:
    root = Path.cwd() / ".pytest-local-code-sandbox" / uuid4().hex
    artifacts_root = root / "artifacts"
    logical_path = "runs/artifacts/tsk_fc_1/firecrawl_web_scrape/page.md"
    stored_path = artifacts_root / "tsk_fc_1" / "firecrawl_web_scrape" / "page.md"
    stored_path.parent.mkdir(parents=True, exist_ok=True)
    stored_path.write_text("# Portfolio\n\nFull markdown body.", encoding="utf-8")
    try:
        executor = ToolExecutor(artifacts_root=artifacts_root)
        raw_result = await executor.execute(
            "artifact_read",
            {
                "path": logical_path,
                "artifact_id": "art_fc_1",
            },
            context=ToolExecutionContext(task_id="tsk_fc_1", session_id="sess_fc_1"),
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)

    result = json.loads(raw_result)
    assert result["found"] is True
    assert "Full markdown body." in result["content"]
    assert result["truncated"] is False


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
async def test_tool_executor_heartbeat_notes_manages_private_markdown(tmp_path: Path) -> None:
    notes_path = tmp_path / "heartbeat_notes.md"
    executor = ToolExecutor(heartbeat_notes_path=notes_path)

    read_initial = json.loads(await executor.execute("heartbeat_notes", {"action": "read"}))
    assert read_initial["updated"] is False
    assert read_initial["content"].startswith("# COSMIC Heartbeat Notes")

    appended = json.loads(
        await executor.execute(
            "heartbeat_notes",
            {
                "action": "append",
                "content": "- Recheck active portfolio polish on a future beat.",
            },
        )
    )
    assert appended["updated"] is True
    assert "portfolio polish" in appended["content"]
    assert notes_path.read_text(encoding="utf-8") == appended["content"]

    removed = json.loads(
        await executor.execute(
            "heartbeat_notes",
            {
                "action": "remove",
                "match": "- Recheck active portfolio polish on a future beat.",
            },
        )
    )
    assert removed["updated"] is True
    assert "portfolio polish" not in removed["content"]

    replaced = json.loads(
        await executor.execute(
            "heartbeat_notes",
            {"action": "replace", "content": "- Watch YC and AI research when current context warrants it."},
        )
    )
    assert replaced["updated"] is True
    assert replaced["content"].startswith("# COSMIC Heartbeat Notes")
    assert "Watch YC" in replaced["content"]


@pytest.mark.asyncio
async def test_tool_executor_perplexity_research_posts_usage_to_gateway() -> None:
    usage_posts: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL("https://api.perplexity.ai/chat/completions"):
            return httpx.Response(
                200,
                headers={"x-request-id": "pplx_req_1"},
                json={
                    "usage": {
                        "prompt_tokens": 15,
                        "completion_tokens": 6,
                        "total_tokens": 21,
                    },
                    "choices": [
                        {
                            "message": {
                                "content": "Answer with citations.",
                            }
                        }
                    ],
                    "citations": ["https://example.com/source"],
                },
            )
        if request.url == httpx.URL("http://gateway/internal/usage/log"):
            usage_posts.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(201, json={"ok": True, "deduplicated": False})
        raise AssertionError(f"Unexpected request URL: {request.url!s}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    executor = ToolExecutor(
        perplexity_api_key="perplexity-key",
        gateway_url="http://gateway",
        gateway_internal_token="internal-token",
        client=client,
    )
    context = ToolExecutionContext(
        task_id="tsk_1",
        request_id="req_1",
        session_id="sess_1",
        channel="desktop:desk_a",
        source="user",
        source_id="desktop",
    )
    try:
        raw_result = await executor.execute(
            "perplexity_research",
            {"query": "latest AI updates"},
            context=context,
        )
    finally:
        await client.aclose()

    result = json.loads(raw_result)
    assert result["answer"] == "Answer with citations."
    assert result["citations"] == ["https://example.com/source"]
    assert len(usage_posts) == 1
    usage_event = usage_posts[0]
    assert usage_event["provider"] == "perplexity"
    assert usage_event["model"] == "sonar"
    assert usage_event["operation"] == "orchestrator.perplexity_research"
    assert usage_event["task_id"] == "tsk_1"
    assert usage_event["session_id"] == "sess_1"
    assert usage_event["request_id"] == "req_1"
    assert usage_event["provider_request_id"] == "pplx_req_1"
    assert usage_event["prompt_tokens"] == 15
    assert usage_event["completion_tokens"] == 6
    assert usage_event["total_tokens"] == 21
    assert usage_event["metadata_json"]["tool"] == "perplexity_research"


@pytest.mark.asyncio
async def test_tool_executor_event_automation_crud_uses_gateway_routes() -> None:
    seen_payload: dict[str, object] = {}
    seen_delete = ""

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_delete
        assert request.headers["X-Internal-Token"] == "internal-token"
        if request.url.path == "/internal/automations/events":
            if request.method == "POST":
                seen_payload.update(json.loads(request.content.decode("utf-8")))
                return httpx.Response(
                    200,
                    json={
                        "automation_id": "aut_arun_doc",
                        "event_type": "gmail.inbound",
                        "label": "Arun doc request",
                        "raw_instruction": "When Arun emails me, create the requested doc.",
                        "condition": {"person_ref": "Arun", "resolution_mode": "resolve_on_event"},
                        "action": {"type": "orchestrator_task", "goal": "Read the thread and create the doc."},
                        "approval_policy": {"send_email": "requires_approval"},
                        "status": "active",
                    },
                )
            if request.method == "GET":
                assert request.url.params["event_type"] == "gmail.inbound"
                assert request.url.params["status_filter"] == "active"
                return httpx.Response(
                    200,
                    json={
                        "automations": [
                            {
                                "automation_id": "aut_arun_doc",
                                "event_type": "gmail.inbound",
                                "label": "Arun doc request",
                                "status": "active",
                            }
                        ]
                    },
                )
        if request.url == httpx.URL("http://gateway/internal/automations/events/aut_arun_doc"):
            assert request.method == "DELETE"
            seen_delete = request.url.path
            return httpx.Response(200, json={"deleted": True, "automation_id": "aut_arun_doc"})
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
        channel="desktop:desk_a",
    )
    try:
        created_raw = await executor.execute(
            "create_event_automation",
            {
                "event_type": "gmail.inbound",
                "label": "Arun doc request",
                "raw_instruction": "When Arun emails me, create the requested doc.",
                "condition": {"person_ref": "Arun", "resolution_mode": "resolve_on_event"},
                "action": {"type": "orchestrator_task", "goal": "Read the thread and create the doc."},
                "approval_policy": {"send_email": "requires_approval"},
            },
            context=context,
        )
        listed_raw = await executor.execute(
            "list_event_automations",
            {"event_type": "gmail.inbound", "status": "active"},
            context=context,
        )
        deleted_raw = await executor.execute(
            "delete_event_automation",
            {"automation_id": "aut_arun_doc"},
            context=context,
        )
    finally:
        await client.aclose()

    assert seen_payload == {
        "event_type": "gmail.inbound",
        "raw_instruction": "When Arun emails me, create the requested doc.",
        "condition": {"person_ref": "Arun", "resolution_mode": "resolve_on_event"},
        "action": {"type": "orchestrator_task", "goal": "Read the thread and create the doc."},
        "approval_policy": {"send_email": "requires_approval"},
        "status": "active",
        "source": "orchestrator",
        "label": "Arun doc request",
        "request_id": "req_current",
        "session_id": "sess_current",
        "channel": "desktop:desk_a",
    }
    assert json.loads(created_raw)["automation_id"] == "aut_arun_doc"
    assert json.loads(listed_raw)["automations"][0]["label"] == "Arun doc request"
    assert json.loads(deleted_raw)["deleted"] is True
    assert seen_delete == "/internal/automations/events/aut_arun_doc"


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
async def test_tool_executor_capability_wishlist_search_uses_gateway_internal_route() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("http://gateway/internal/cosmics-capability-wishlist/search")
        assert request.headers["X-Internal-Token"] == "internal-token"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload == {"query": "desktop task observability", "limit": 3}
        return httpx.Response(
            200,
            json={
                "query": "desktop task observability",
                "matches": [
                    {
                        "capability_id": "cap_000014",
                        "title": "Spaces control-center task observability",
                        "summary": "Show all active tasks and live task flow from desktop.",
                    }
                ],
                "count": 1,
                "message": "Found 1 matching capability wishlist entry.",
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
            "cosmics_capability_wishlist_search",
            {"query": "desktop task observability", "limit": 3},
        )
    finally:
        await client.aclose()

    result = json.loads(raw_result)
    assert result["matches"][0]["capability_id"] == "cap_000014"
    assert result["matches"][0]["title"] == "Spaces control-center task observability"


@pytest.mark.asyncio
async def test_tool_executor_capability_wishlist_capture_uses_gateway_internal_route_and_context() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("http://gateway/internal/cosmics-capability-wishlist/capture")
        assert request.headers["X-Internal-Token"] == "internal-token"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload == {
            "title": "Cross-channel delivery target resolver",
            "summary": "Let reminders and future actions target a user-linked channel without requiring a raw channel id.",
            "desired_outcome": "Opus can schedule delivery to desktop, WhatsApp, or Telegram through a stable resolver.",
            "domain": "communications",
            "tags": ["channels", "reminders"],
            "evidence": "The user explicitly asked for reminders to reach any linked channel.",
            "source_component": "orchestrator",
            "source_id": "desktop",
            "request_id": "req_parent",
            "session_id": "sess_parent",
            "task_id": "tsk_parent",
            "route": "opus",
            "created_by": "cosmic/orchestrator:1.0.0",
            "metadata": {"task_source": "user", "channel": "desktop:test"},
        }
        return httpx.Response(
            201,
            json={
                "status": "created_new",
                "capability_id": "cap_000201",
                "title": "Cross-channel delivery target resolver",
                "message": "Added new capability wishlist entry cap_000201: Cross-channel delivery target resolver.",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    executor = ToolExecutor(
        gateway_url="http://gateway",
        gateway_internal_token="internal-token",
        client=client,
    )
    context = ToolExecutionContext(
        task_id="tsk_parent",
        request_id="req_parent",
        session_id="sess_parent",
        channel="desktop:test",
        source="user",
        source_id="desktop",
    )
    try:
        raw_result = await executor.execute(
            "cosmics_capability_wishlist_capture",
            {
                "title": "Cross-channel delivery target resolver",
                "summary": "Let reminders and future actions target a user-linked channel without requiring a raw channel id.",
                "desired_outcome": "Opus can schedule delivery to desktop, WhatsApp, or Telegram through a stable resolver.",
                "domain": "communications",
                "tags": ["channels", "reminders"],
                "evidence": "The user explicitly asked for reminders to reach any linked channel.",
            },
            context=context,
        )
    finally:
        await client.aclose()

    result = json.loads(raw_result)
    assert result["status"] == "created_new"
    assert result["capability_id"] == "cap_000201"


@pytest.mark.asyncio
async def test_tool_executor_docs_tools_delegate_to_docs_parser_agent() -> None:
    observed_calls: list[dict[str, object]] = []

    async def dispatcher(**kwargs):
        observed_calls.append(kwargs)
        intent = kwargs["intent"]
        if intent == "docs.browse_bundle":
            return AgentResult(
                status="completed",
                output={
                    "response": "Loaded 1 section index item.",
                    "bundle_id": "bundle_docs_001",
                    "index_kind": "sections",
                    "doc_id": "doc_001",
                    "sections": [{"section_id": "sec_001", "title": "Executive Summary"}],
                },
                artifacts=[],
            )
        if intent == "docs.search_bundle":
            return AgentResult(
                status="completed",
                output={
                    "response": "Found 1 matching chunk.",
                    "bundle_id": "bundle_docs_001",
                    "query": "enterprise pricing",
                    "count": 1,
                    "matches": [{"chunk_id": "chk_001", "excerpt": "Enterprise pricing changed.", "doc_id": "doc_001", "score": 12}],
                },
                artifacts=[],
            )
        if intent == "docs.fetch_asset":
            return AgentResult(
                status="completed",
                output={
                    "response": "Loaded asset asset_tbl_001.",
                    "bundle_id": "bundle_docs_001",
                    "doc_id": "doc_001",
                    "asset_id": "asset_tbl_001",
                    "asset": {"asset_id": "asset_tbl_001", "kind": "table_markdown"},
                    "content": "| Owner |",
                    "path": "runs/artifacts/tsk_docs_parse/docs_parser/art_doc_1/assets/tables/tbl_001.md",
                },
                artifacts=[],
            )
        if intent == "docs.reinspect_asset":
            return AgentResult(
                status="completed",
                output={
                    "response": "Completed visual reinspection for asset asset_fig_001.",
                    "bundle_id": "bundle_docs_001",
                    "doc_id": "doc_001",
                    "asset_id": "asset_fig_001",
                    "asset": {"asset_id": "asset_fig_001", "kind": "figure_image"},
                    "cached": False,
                    "analysis": {
                        "summary": "A bar chart comparing enterprise revenue by quarter.",
                        "visual_type": "chart",
                        "confidence": "high",
                    },
                },
                artifacts=[],
            )
        return AgentResult(
            status="completed",
            output={
                "response": "Loaded section from parsed bundle.",
                "bundle_id": "bundle_docs_001",
                "doc_id": "doc_001",
                "mode": "section",
                "content": "Enterprise pricing changed.",
                "citations": [{"section_id": "sec_001"}],
            },
            artifacts=[],
        )

    executor = ToolExecutor(agent_dispatcher=dispatcher)
    context = ToolExecutionContext(parent_task=_parent_task(), session_id="sess_parent", task_id="tsk_parent")

    browse_result = json.loads(
        await executor.execute(
            "docs_browse",
            {"bundle_id": "bundle_docs_001", "index_kind": "sections", "doc_id": "doc_001"},
            context=context,
        )
    )
    search_result = json.loads(
        await executor.execute(
            "docs_search",
            {"bundle_id": "bundle_docs_001", "query": "enterprise pricing"},
            context=context,
        )
    )
    read_result = json.loads(
        await executor.execute(
            "docs_read",
            {"bundle_id": "bundle_docs_001", "doc_id": "doc_001", "section_id": "sec_001"},
            context=context,
        )
    )
    asset_result = json.loads(
        await executor.execute(
            "docs_fetch_asset",
            {"bundle_id": "bundle_docs_001", "doc_id": "doc_001", "asset_id": "asset_tbl_001"},
            context=context,
        )
    )
    reinspect_result = json.loads(
        await executor.execute(
            "docs_reinspect_asset",
            {
                "bundle_id": "bundle_docs_001",
                "doc_id": "doc_001",
                "asset_id": "asset_fig_001",
                "question": "What does the chart compare?",
            },
            context=context,
        )
    )

    assert [call["intent"] for call in observed_calls] == [
        "docs.browse_bundle",
        "docs.search_bundle",
        "docs.read_bundle",
        "docs.fetch_asset",
        "docs.reinspect_asset",
    ]
    assert observed_calls[0]["agent_id"] == "cosmic/docs-parser-agent:1.0.0"
    assert observed_calls[1]["input_payload"] == {
        "bundle_id": "bundle_docs_001",
        "query": "enterprise pricing",
        "limit": 5,
    }
    assert observed_calls[2]["input_payload"] == {
        "bundle_id": "bundle_docs_001",
        "doc_id": "doc_001",
        "section_id": "sec_001",
        "max_chars": 5000,
    }
    assert observed_calls[3]["input_payload"] == {
        "bundle_id": "bundle_docs_001",
        "asset_id": "asset_tbl_001",
        "doc_id": "doc_001",
        "max_chars": 5000,
    }
    assert observed_calls[4]["input_payload"] == {
        "bundle_id": "bundle_docs_001",
        "asset_id": "asset_fig_001",
        "doc_id": "doc_001",
        "question": "What does the chart compare?",
    }
    assert browse_result["index_kind"] == "sections"
    assert search_result["count"] == 1
    assert read_result["mode"] == "section"
    assert asset_result["asset_id"] == "asset_tbl_001"
    assert reinspect_result["analysis"]["visual_type"] == "chart"


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
                "_cosmic_ui": {"render": "trusted_inline_block", "block_type": "untrusted"},
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
    assert result["artifacts_ready_in_response"] is True
    assert result["artifact_count"] == 1
    assert result["delegation"] == {
        "intent": "firecrawl.scrape",
        "agent_id": "cosmic/firecrawl-web-scrape-agent:1.0.0",
        "task_id": None,
    }
    assert "_cosmic_ui" not in result


@pytest.mark.asyncio
async def test_tool_executor_marks_gmail_draft_for_trusted_inline_presentation() -> None:
    async def dispatcher(**kwargs):
        del kwargs
        return AgentResult(
            status="completed",
            output={
                "status": "draft_created",
                "account": {
                    "account_id": "acct_123",
                    "account_email": "owner@example.com",
                },
                "draft_id": "draft_123",
                "approval_required": True,
                "draft": {
                    "to": ["recipient@example.com"],
                    "subject": "Follow-up",
                    "body": "Hi, following up.",
                },
            },
            artifacts=[],
        )

    executor = ToolExecutor(agent_dispatcher=dispatcher)
    raw_result = await executor.execute(
        "delegate_to_agent",
        {
            "intent": "gmail.draft_reply",
            "input": {"request": "Draft a follow-up."},
        },
        context=ToolExecutionContext(
            channel="desktop:test",
            parent_task=_parent_task(),
        ),
    )

    result = json.loads(raw_result)
    assert result["_cosmic_ui"]["render"] == "trusted_inline_block"
    assert result["_cosmic_ui"]["block_type"] == "gmail_draft_approval"
    assert result["_cosmic_ui"]["response_mode"] == "brief_acknowledgement"
    assert "email body" in result["_cosmic_ui"]["covers"]
    assert "Do not repeat" in result["_cosmic_ui"]["instruction"]


@pytest.mark.asyncio
async def test_tool_executor_does_not_suppress_gmail_details_on_non_ui_channel() -> None:
    async def dispatcher(**kwargs):
        del kwargs
        return AgentResult(
            status="completed",
            output={
                "account": {"account_id": "acct_123"},
                "draft_id": "draft_123",
                "approval_required": True,
                "draft": {
                    "to": ["recipient@example.com"],
                    "subject": "Follow-up",
                    "body": "Hi, following up.",
                },
            },
            artifacts=[],
        )

    executor = ToolExecutor(agent_dispatcher=dispatcher)
    raw_result = await executor.execute(
        "delegate_to_agent",
        {
            "intent": "gmail.draft_reply",
            "input": {"request": "Draft a follow-up."},
        },
        context=ToolExecutionContext(
            channel="telegram:123",
            parent_task=_parent_task(),
        ),
    )

    assert "_cosmic_ui" not in json.loads(raw_result)


def test_tool_executor_marks_calendar_action_for_trusted_inline_presentation() -> None:
    contract = ToolExecutor._trusted_inline_presentation_contract(  # noqa: SLF001
        intent="calendar.create_event",
        response={
            "event": {
                "event_id": "evt_123",
                "summary": "Design review",
            }
        },
        context=ToolExecutionContext(channel="mobile:device_123"),
    )

    assert contract is not None
    assert contract["block_type"] == "calendar_event"
    assert contract["response_mode"] == "brief_acknowledgement"
    assert "event title" in contract["covers"]


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
        "delegation": {"intent": "firecrawl.extract", "agent_id": None, "task_id": None},
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
        "delegation": {
            "intent": "firecrawl.recall_session",
            "agent_id": None,
            "task_id": "tsk_child_firecrawl",
        },
    }


@pytest.mark.asyncio
async def test_tool_executor_alpha_delegate_externalizes_large_payload_and_inherits_artifacts(tmp_path) -> None:
    observed: dict[str, object] = {}

    async def dispatcher(**kwargs):
        observed.update(kwargs)
        return AgentResult(status="completed", output={"message": "ok"}, artifacts=[])

    inherited_artifact = {
        "artifact_id": "art_resume_pdf",
        "mime": "application/pdf",
        "path": "runs/artifacts/req_upload/inputs/art_resume_pdf/original/resume.pdf",
        "filename": "resume.pdf",
        "parse_bundle_id": "bundle_resume_1",
        "parsed_summary": {
            "doc_id": "doc_resume_1",
            "filename": "resume.pdf",
            "paths": {
                "document_md": "runs/artifacts/tsk_docs_parse/docs_parser/art_resume_pdf/document.md",
                "chunk_index": "runs/artifacts/tsk_docs_parse/docs_parser/art_resume_pdf/chunk_index.json",
                "manifest": "runs/artifacts/tsk_docs_parse/docs_parser/art_resume_pdf/manifest.json",
            },
        },
    }
    parent_task = _parent_task().model_copy(update={"input_artifacts": [inherited_artifact]})
    executor = ToolExecutor(
        artifacts_root=tmp_path / "artifacts",
        agent_dispatcher=dispatcher,
    )
    large_goal = "portfolio resume context\n" + ("Principal engineer accomplishments.\n" * 1200)
    context = ToolExecutionContext(
        task_id="tsk_parent",
        request_id="req_parent",
        session_id="sess_parent",
        channel="desktop:test",
        source="user",
        source_id="desktop",
        parent_task=parent_task,
    )

    raw_result = await executor.execute(
        "delegate_to_agent",
        {
            "intent": "alpha.execute",
            "input": {
                "goal": large_goal,
                "preferred_harness": "cursor",
            },
        },
        context=context,
    )

    result = json.loads(raw_result)
    assert result["message"] == "ok"
    input_payload = observed["input_payload"]
    assert isinstance(input_payload, dict)
    assert "Large Alpha input moved to artifact" in input_payload["goal"]
    assert "Principal engineer accomplishments" not in input_payload["goal"]
    input_artifacts = observed["input_artifacts"]
    assert isinstance(input_artifacts, list)
    assert inherited_artifact in input_artifacts
    parsed_bundle_artifacts = [
        item for item in input_artifacts
        if item.get("kind") == "parsed_document_bundle"
    ]
    assert {item["bundle_path_key"] for item in parsed_bundle_artifacts} == {
        "document_md",
        "chunk_index",
        "manifest",
    }
    assert all(item["parse_bundle_id"] == "bundle_resume_1" for item in parsed_bundle_artifacts)
    assert any(item["mime"] == "text/markdown" for item in parsed_bundle_artifacts)
    assert any(item["mime"] == "application/json" for item in parsed_bundle_artifacts)
    externalized = [item for item in input_artifacts if item.get("artifact_id", "").startswith("art_alpha_input_")]
    assert len(externalized) == 1
    externalized_path = tmp_path / "artifacts" / "alpha_handoffs" / "tsk_parent"
    assert str(externalized[0]["path"]).startswith(str(externalized_path))
    assert Path(externalized[0]["path"]).read_text(encoding="utf-8") == large_goal


@pytest.mark.asyncio
async def test_tool_executor_artifact_lookup_and_redeliver_use_gateway_routes() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL("http://gateway/internal/session/artifacts/search"):
            payload = json.loads(request.content.decode("utf-8"))
            assert payload == {
                "session_id": "sess_current",
                "query": "yc spreadsheet",
                "limit": 4,
                "all_sessions": True,
            }
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "artifact_id": "out_yc_1",
                            "filename": "YC_Spring_2026_Companies.xlsx",
                            "session_id": "sess_current",
                            "downloadable": True,
                        }
                    ],
                    "count": 1,
                },
            )
        if request.url == httpx.URL("http://gateway/internal/session/artifacts/resolve"):
            payload = json.loads(request.content.decode("utf-8"))
            assert payload == {
                "session_id": "sess_current",
                "artifact_ids": ["out_yc_1"],
                "all_sessions": False,
            }
            return httpx.Response(
                200,
                json={
                    "artifacts": [
                        {
                            "artifact_id": "out_yc_1",
                            "task_id": "tsk_export",
                            "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            "path": "runs/artifacts/tsk_export/out/YC_Spring_2026_Companies.xlsx",
                            "filename": "YC_Spring_2026_Companies.xlsx",
                            "audience": "deliverable",
                        }
                    ],
                    "count": 1,
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    executor = ToolExecutor(
        gateway_url="http://gateway",
        gateway_internal_token="internal-token",
        client=client,
    )
    context = ToolExecutionContext(session_id="sess_current")
    try:
        lookup_raw = await executor.execute(
            "artifact_lookup",
            {"query": "yc spreadsheet", "limit": 4, "all_sessions": True},
            context=context,
        )
        redeliver_raw = await executor.execute(
            "artifact_redeliver",
            {"artifact_id": "out_yc_1"},
            context=context,
        )
    finally:
        await client.aclose()

    lookup_result = json.loads(lookup_raw)
    redeliver_result = json.loads(redeliver_raw)
    assert lookup_result["results"][0]["artifact_id"] == "out_yc_1"
    assert redeliver_result["found"] is True
    assert redeliver_result["artifacts"][0]["filename"] == "YC_Spring_2026_Companies.xlsx"


@pytest.mark.asyncio
async def test_tool_executor_delegate_to_agent_resolves_artifact_ids_into_input_artifacts() -> None:
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL("http://gateway/internal/session/artifacts/resolve"):
            payload = json.loads(request.content.decode("utf-8"))
            assert payload == {
                "session_id": "sess_parent",
                "artifact_ids": ["out_prev_1"],
                "all_sessions": False,
            }
            return httpx.Response(
                200,
                json={
                    "artifacts": [
                        {
                            "artifact_id": "out_prev_1",
                            "task_id": "tsk_prev_export",
                            "mime": "text/csv",
                            "path": "runs/artifacts/tsk_prev_export/out/list.csv",
                            "filename": "list.csv",
                            "sha256": "abc123",
                            "audience": "deliverable",
                        }
                    ],
                    "count": 1,
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async def dispatcher(**kwargs):
        observed.update(kwargs)
        return AgentResult(status="completed", output={"message": "ok"}, artifacts=[])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    executor = ToolExecutor(
        gateway_url="http://gateway",
        gateway_internal_token="internal-token",
        agent_dispatcher=dispatcher,
        client=client,
    )
    context = ToolExecutionContext(parent_task=_parent_task(), session_id="sess_parent")
    try:
        raw_result = await executor.execute(
            "delegate_to_agent",
            {
                "intent": "docs.parse_bundle",
                "input": {"bundle_label": "parse this prior file again"},
                "artifact_ids": ["out_prev_1"],
            },
            context=context,
        )
    finally:
        await client.aclose()

    result = json.loads(raw_result)
    assert observed["input_artifacts"] == [
        {
            "artifact_id": "out_prev_1",
            "task_id": "tsk_prev_export",
            "mime": "text/csv",
            "path": "runs/artifacts/tsk_prev_export/out/list.csv",
            "filename": "list.csv",
            "sha256": "abc123",
            "audience": "deliverable",
        }
    ]
    assert result["message"] == "ok"


@pytest.mark.asyncio
async def test_tool_executor_delegate_rehydrates_transient_explicit_input_artifact() -> None:
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL("http://gateway/internal/session/artifacts/resolve"):
            payload = json.loads(request.content.decode("utf-8"))
            assert payload == {
                "session_id": "email-thread:arun",
                "artifact_ids": ["anthropic_file_pdf_1"],
                "all_sessions": True,
            }
            return httpx.Response(
                200,
                json={
                    "artifacts": [
                        {
                            "artifact_id": "anthropic_file_pdf_1",
                            "task_id": "tsk_code_output",
                            "mime": "application/pdf",
                            "path": "runs/artifacts/tsk_code_output/orchestrator/anthropic_code_execution/file_pdf_1__Strategy.pdf",
                            "filename": "Strategy.pdf",
                            "sha256": "abc123",
                            "audience": "deliverable",
                        }
                    ],
                    "count": 1,
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async def dispatcher(**kwargs):
        observed.update(kwargs)
        return AgentResult(status="completed", output={"message": "ok"}, artifacts=[])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    executor = ToolExecutor(
        gateway_url="http://gateway",
        gateway_internal_token="internal-token",
        agent_dispatcher=dispatcher,
        client=client,
    )
    context = ToolExecutionContext(parent_task=_parent_task(), session_id="email-thread:arun")
    try:
        raw_result = await executor.execute(
            "delegate_to_agent",
            {
                "intent": "email.handle",
                "input": {"goal": "Email the doc to Arun."},
                "input_artifacts": [
                    {
                        "artifact_id": "anthropic_file_pdf_1",
                        "mime": "application/pdf",
                        "path": "/files/input/stale/Strategy.pdf",
                        "filename": "Strategy.pdf",
                    }
                ],
            },
            context=context,
        )
    finally:
        await client.aclose()

    result = json.loads(raw_result)
    assert result["message"] == "ok"
    assert observed["input_artifacts"] == [
        {
            "artifact_id": "anthropic_file_pdf_1",
            "task_id": "tsk_code_output",
            "mime": "application/pdf",
            "path": "runs/artifacts/tsk_code_output/orchestrator/anthropic_code_execution/file_pdf_1__Strategy.pdf",
            "filename": "Strategy.pdf",
            "sha256": "abc123",
            "audience": "deliverable",
        }
    ]


@pytest.mark.asyncio
async def test_heartbeat_notes_append_survives_the_document_cap(tmp_path: Path) -> None:
    """Past the cap, the old head-truncating write silently discarded appends.

    The scratchpad became append-proof with no error anywhere: the tool still
    reported success, the file just never gained the new note. The live file was
    growing ~4KB/day against a 32k cap when this was found.
    """
    from orchestrator.tools.executor import HEARTBEAT_NOTES_MAX_CHARS

    notes_path = tmp_path / "heartbeat_notes.md"
    notes_path.write_text(
        "# COSMIC Heartbeat Notes\n\n"
        "## Active watchpoints\n- lease runs to Dec 8 2026\n\n"
        + ("- old filler note\n" * ((HEARTBEAT_NOTES_MAX_CHARS // 18) + 500)),
        encoding="utf-8",
    )
    assert notes_path.stat().st_size > HEARTBEAT_NOTES_MAX_CHARS

    executor = ToolExecutor(heartbeat_notes_path=notes_path)
    result = json.loads(
        await executor.execute(
            "heartbeat_notes", {"action": "append", "content": "- 8/9 THE NEW NOTE"}
        )
    )

    assert result["updated"] is True
    written = notes_path.read_text(encoding="utf-8")
    assert "THE NEW NOTE" in written, "an append past the cap must not be discarded"
    assert "lease runs to Dec 8 2026" in written, "standing state must survive too"
    assert len(written) <= HEARTBEAT_NOTES_MAX_CHARS + len("# COSMIC Heartbeat Notes\n\n") + 2
