from __future__ import annotations

import json
from pathlib import Path
from shutil import rmtree
from uuid import uuid4

import httpx
import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.runtime import OrchestratorRuntime
from orchestrator.tools.executor import ToolExecutor
from orchestrator.tools.registry import (
    build_tool_prompt_catalog,
    get_local_tool_definitions,
    get_tool_spec,
)
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


def _with_model_selection(task: TaskEnvelope, provider: str, model: str) -> TaskEnvelope:
    task = task.model_copy(
        update={
            "input": {
                **task.input,
                "cosmic_orchestrator_model": {"provider": provider, "model": model},
            }
        }
    )
    return task.model_copy(update={"signature": sign_task_envelope(task, "signing-secret")})


def _sse(chunks: list[bytes]) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=SSEByteStream(chunks),
    )


def test_think_deeper_is_registered_as_local_parallel_safe_tool() -> None:
    spec = get_tool_spec("think_deeper")
    assert spec is not None
    assert spec.is_local
    assert spec.read_only
    assert spec.handler_method == "_think_deeper"
    local_names = {definition["name"] for definition in get_local_tool_definitions()}
    assert "think_deeper" in local_names
    catalog = build_tool_prompt_catalog()
    assert "`think_deeper`" in catalog
    assert "Thinking" in catalog


@pytest.mark.asyncio
async def test_tool_executor_think_deeper_accepts_named_effort() -> None:
    executor = ToolExecutor()
    result = json.loads(await executor.execute("think_deeper", {"effort": "max", "reason": "tricky"}))
    assert result.get("error") is not True
    assert result["status"] == "ok"
    assert result["reasoning_effort"] == "max"


@pytest.mark.asyncio
async def test_tool_executor_think_deeper_accepts_token_budget() -> None:
    executor = ToolExecutor()
    result = json.loads(await executor.execute("think_deeper", {"budget_tokens": 4096, "reason": "hard bug"}))
    assert result.get("error") is not True
    assert result["reasoning_effort"] == 4096
    assert result["scope"] == "subsequent model calls in this turn"


@pytest.mark.asyncio
async def test_tool_executor_think_deeper_rejects_missing_params() -> None:
    executor = ToolExecutor()
    result = json.loads(await executor.execute("think_deeper", {"reason": "no level given"}))
    assert result.get("error") is True


@pytest.mark.asyncio
async def test_tool_executor_think_deeper_rejects_both_params() -> None:
    executor = ToolExecutor()
    result = json.loads(
        await executor.execute(
            "think_deeper",
            {"effort": "max", "budget_tokens": 4096, "reason": "both"},
        )
    )
    assert result.get("error") is True
    assert "not both" in result["message"]


@pytest.mark.asyncio
async def test_tool_executor_think_deeper_rejects_bad_level() -> None:
    executor = ToolExecutor()
    result = json.loads(await executor.execute("think_deeper", {"effort": "turbo", "reason": "x"}))
    assert result.get("error") is True
    assert "low, medium, high, max" in result["message"]


@pytest.mark.asyncio
async def test_tool_executor_think_deeper_rejects_out_of_range_budget() -> None:
    executor = ToolExecutor()
    result = json.loads(await executor.execute("think_deeper", {"budget_tokens": 99999, "reason": "big"}))
    assert result.get("error") is True
    assert "between 256 and 32768" in result["message"]


@pytest.mark.asyncio
async def test_glm_body_carries_default_reasoning_effort(tmp_path: Path) -> None:
    runtime_root = tmp_path / "reasoning_glm"
    request_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/chat/completions"):
            payload = json.loads(request.content.decode("utf-8"))
            request_bodies.append(payload)
            tool_names = {
                tool["function"]["name"]
                for tool in payload.get("tools", [])
                if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
            }
            assert "think_deeper" in tool_names
            return _sse(
                [
                    b'data: {"choices":[{"delta":{"content":"Hello from GLM."},"finish_reason":null}]}\n\n',
                    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":11,"completion_tokens":4,"total_tokens":15}}\n\n',
                    b"data: [DONE]\n\n",
                ]
            )
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="",
        fireworks_api_key="fireworks-key",
        task_ledger_db_path=runtime_root / "task_ledger.db",
    )
    runtime = OrchestratorRuntime(config, client=client)
    await runtime.start()
    try:
        task = _signed_task("signing-secret")
        task = _with_model_selection(task, "fireworks_glm", "accounts/fireworks/models/glm-5p3")
        streamed_events = [event async for event in runtime.stream_task(task)]
    finally:
        await runtime.stop()
        rmtree(runtime_root, ignore_errors=True)

    assert len(request_bodies) >= 1
    assert request_bodies[0]["reasoning_effort"] == "high"
    complete = next(e for e in streamed_events if e["type"] == "response.complete")
    assert complete["metrics"]["reasoning_effort"] == "high"
    assert complete["metrics"]["reasoning_escalations"] == 0


@pytest.mark.asyncio
async def test_think_deeper_escalation_applies_to_next_request(tmp_path: Path) -> None:
    runtime_root = tmp_path / "reasoning_escalation"
    request_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/chat/completions"):
            payload = json.loads(request.content.decode("utf-8"))
            request_bodies.append(payload)
            if len(request_bodies) == 1:
                return _sse(
                    [
                        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
                        b'"function":{"name":"think_deeper","arguments":"{\\"budget_tokens\\": 4096, \\"reason\\": \\"surprising result\\"}"}}]},'
                        b'"finish_reason":null}]}\n\n',
                        b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":50,"completion_tokens":10,"total_tokens":60}}\n\n',
                        b"data: [DONE]\n\n",
                    ]
                )
            return _sse(
                [
                    b'data: {"choices":[{"delta":{"content":"Now I solved it."},"finish_reason":null}]}\n\n',
                    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":50,"completion_tokens":5,"total_tokens":55}}\n\n',
                    b"data: [DONE]\n\n",
                ]
            )
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="",
        fireworks_api_key="fireworks-key",
        task_ledger_db_path=runtime_root / "task_ledger.db",
    )
    runtime = OrchestratorRuntime(config, client=client)
    await runtime.start()
    try:
        task = _with_model_selection(
            _signed_task("signing-secret"), "fireworks_glm", "accounts/fireworks/models/glm-5p3"
        )
        streamed_events = [event async for event in runtime.stream_task(task)]
    finally:
        await runtime.stop()
        rmtree(tmp_path, ignore_errors=True)

    assert len(request_bodies) == 2
    assert request_bodies[0]["reasoning_effort"] == "high"
    assert request_bodies[1]["reasoning_effort"] == 4096
    complete = next(e for e in streamed_events if e["type"] == "response.complete")
    assert complete["metrics"]["reasoning_effort"] == 4096
    assert complete["metrics"]["reasoning_escalations"] == 1
    tool_results = [
        e for e in streamed_events if e["type"] == "tool.result" and e.get("tool_name") == "think_deeper"
    ]
    assert len(tool_results) == 1
    assert "ok" in str(tool_results[0].get("result_preview") or "")


@pytest.mark.asyncio
async def test_think_deeper_invalid_args_keep_default_effort(tmp_path: Path) -> None:
    runtime_root = tmp_path / "reasoning_invalid"
    request_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/chat/completions"):
            payload = json.loads(request.content.decode("utf-8"))
            request_bodies.append(payload)
            if len(request_bodies) == 1:
                return _sse(
                    [
                        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
                        b'"function":{"name":"think_deeper","arguments":"{\\"effort\\": \\"turbo\\", \\"reason\\": \\"why not\\"}"}}]},'
                        b'"finish_reason":null}]}\n\n',
                        b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":50,"completion_tokens":10,"total_tokens":60}}\n\n',
                        b"data: [DONE]\n\n",
                    ]
                )
            return _sse(
                [
                    b'data: {"choices":[{"delta":{"content":"Answered anyway."},"finish_reason":null}]}\n\n',
                    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":50,"completion_tokens":5,"total_tokens":55}}\n\n',
                    b"data: [DONE]\n\n",
                ]
            )
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="",
        fireworks_api_key="fireworks-key",
        task_ledger_db_path=runtime_root / "task_ledger.db",
    )
    runtime = OrchestratorRuntime(config, client=client)
    await runtime.start()
    try:
        task = _with_model_selection(
            _signed_task("signing-secret"), "fireworks_glm", "accounts/fireworks/models/glm-5p3"
        )
        streamed_events = [event async for event in runtime.stream_task(task)]
    finally:
        await runtime.stop()
        rmtree(runtime_root, ignore_errors=True)

    assert len(request_bodies) == 2
    assert request_bodies[0]["reasoning_effort"] == "high"
    assert request_bodies[1]["reasoning_effort"] == "high"
    complete = next(e for e in streamed_events if e["type"] == "response.complete")
    assert complete["content"] == "Answered anyway."
    assert complete["metrics"]["reasoning_escalations"] == 0


@pytest.mark.asyncio
async def test_kimi_path_request_body_has_no_reasoning_effort(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/chat/completions"):
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["model"] == "accounts/fireworks/models/kimi-k2p6"
            assert "reasoning_effort" not in payload
            return _sse(
                [
                    b'data: {"choices":[{"delta":{"content":"Hello from Kimi."},"finish_reason":null}]}\n\n',
                    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":4,"total_tokens":14}}\n\n',
                    b"data: [DONE]\n\n",
                ]
            )
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="",
        fireworks_api_key="fireworks-key",
        task_ledger_db_path=tmp_path / "task_ledger_kimi.db",
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
        rmtree(tmp_path, ignore_errors=True)

    complete = next(e for e in streamed_events if e["type"] == "response.complete")
    assert complete["content"] == "Hello from Kimi."
