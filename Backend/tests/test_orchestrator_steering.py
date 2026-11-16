"""Orchestrator side of mid-run user messages (steering)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.runtime import OrchestratorRuntime
from orchestrator.steering import (
    MAX_STEER_CHARS,
    STEER_PREAMBLE,
    SteerInbox,
    last_message_accepts_user_text,
    render_steers_for_model,
)
from test_orchestrator_runtime import _signed_task


def _anthropic_tool_turn() -> list[tuple[str, dict]]:
    return [
        ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "tool_1", "name": "delegate_to_agent"}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{\"intent\":\"alpha.execute\",\"input\":{\"goal\":\"probe\"}}"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}}),
        ("message_stop", {"type": "message_stop"}),
    ]


def _anthropic_final_turn(text: str) -> list[tuple[str, dict]]:
    return [
        ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 12}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}}),
        ("message_stop", {"type": "message_stop"}),
    ]


def _runtime(tmp_path, *, provider: str = "anthropic") -> OrchestratorRuntime:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    config = OrchestratorConfig(
        internal_token="internal-token",
        signing_secret="signing-secret",
        anthropic_api_key="anthropic-key",
        anthropic_model="claude-opus-4-6",
        orchestrator_default_provider=provider,
        fireworks_api_key="fw-key",
        task_ledger_db_path=tmp_path / f"task_ledger_steer_{provider}.db",
    )
    return OrchestratorRuntime(config, client=client)


def _delegation_result() -> str:
    return json.dumps(
        {
            "delegated": True,
            "delegation": {"intent": "alpha.execute", "agent_id": "cosmic/alpha-agent:1.0.0", "task_id": "tsk_alpha"},
        }
    )


@pytest.mark.asyncio
async def test_steer_sent_during_a_tool_call_reaches_the_next_model_turn(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    task = _signed_task("signing-secret")
    seen_messages: list[list[dict]] = []

    async def scripted_stream(*, messages, **_kwargs):
        seen_messages.append(json.loads(json.dumps(messages)))
        events = _anthropic_tool_turn() if len(seen_messages) == 1 else _anthropic_final_turn("Done, nonstop only.")
        for name, payload in events:
            yield type("SSE", (), {"event": name, "data": json.dumps(payload)})()

    async def fake_execute(tool_name, tool_input, *, context=None):
        # The user types while the tool is running.
        assert runtime.submit_user_steer(task.task_id, message_id="req_steer_1", content="only nonstop flights")
        assert runtime.submit_user_steer(task.task_id, message_id="req_steer_1", content="only nonstop flights")  # idempotent
        return _delegation_result()

    runtime._stream_anthropic_events = scripted_stream  # type: ignore[method-assign]
    await runtime.start()
    runtime._tool_executor.execute = fake_execute  # type: ignore[method-assign,union-attr]
    try:
        events = [event async for event in runtime.stream_task(task)]
    finally:
        await runtime.stop()

    assert len(seen_messages) == 2
    first_turn_text = json.dumps(seen_messages[0])
    assert "only nonstop flights" not in first_turn_text
    last_user = seen_messages[1][-1]
    assert last_user["role"] == "user"
    blocks = last_user["content"]
    assert blocks[0]["type"] == "tool_result"  # tool results stay first
    steer_blocks = [block for block in blocks if block.get("type") == "text" and "only nonstop flights" in block.get("text", "")]
    assert len(steer_blocks) == 1
    assert steer_blocks[0]["text"].startswith(STEER_PREAMBLE)

    delivered = [event for event in events if event["type"] == "task.user_message_delivered"]
    assert len(delivered) == 1 and delivered[0]["message_ids"] == ["req_steer_1"]
    delivered_index = events.index(delivered[0])
    completed_index = next(i for i, e in enumerate(events) if e["type"] == "task.completed")
    assert delivered_index < completed_index
    assert delivered[0]["request_id"] == "req_test123"


@pytest.mark.asyncio
async def test_steer_arriving_after_the_last_boundary_is_refused(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    task = _signed_task("signing-secret")
    results: list[bool] = []

    async def scripted_stream(*, messages, **_kwargs):
        # Final turn is streaming: a steer now cannot reach this run.
        for name, payload in _anthropic_final_turn("All done."):
            yield type("SSE", (), {"event": name, "data": json.dumps(payload)})()
        results.append(runtime.submit_user_steer(task.task_id, message_id="late", content="too late"))

    runtime._stream_anthropic_events = scripted_stream  # type: ignore[method-assign]
    await runtime.start()
    try:
        events = []
        async for event in runtime.stream_task(task):
            events.append(event)
            if event["type"] == "task.completed":
                # Inbox closed before the terminal event.
                results.append(runtime.submit_user_steer(task.task_id, message_id="later", content="x"))
    finally:
        await runtime.stop()

    # Accepted while the final turn streamed (inbox still open), but never
    # delivered: the gateway re-dispatches it. After close: refused.
    assert results == [True, False]
    assert not [event for event in events if event["type"] == "task.user_message_delivered"]


@pytest.mark.asyncio
async def test_steer_is_delivered_on_the_openai_compatible_path(tmp_path) -> None:
    runtime = _runtime(tmp_path, provider="fireworks_glm")
    task = _signed_task("signing-secret")
    seen: list[list[dict]] = []

    async def scripted_stream(**kwargs):
        seen.append(json.loads(json.dumps(kwargs.get("messages") or [])))
        if len(seen) == 1:
            payloads = [
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "delegate_to_agent", "arguments": "{\"intent\":\"alpha.execute\",\"input\":{\"goal\":\"probe\"}}"}}]}}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ]
        else:
            payloads = [
                {"choices": [{"delta": {"role": "assistant", "content": "Switched to the EU region."}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ]
        for payload in payloads:
            yield payload

    async def fake_execute(tool_name, tool_input, *, context=None):
        assert runtime.submit_user_steer(task.task_id, message_id="req_s", content="use the EU region")
        return _delegation_result()

    runtime._stream_openai_chat_events = scripted_stream  # type: ignore[method-assign]
    await runtime.start()
    runtime._tool_executor.execute = fake_execute  # type: ignore[method-assign,union-attr]
    try:
        events = [event async for event in runtime.stream_task(task)]
    finally:
        await runtime.stop()

    assert len(seen) == 2
    tool_index = max(i for i, m in enumerate(seen[1]) if m.get("role") == "tool")
    after_tool = seen[1][tool_index + 1:]
    assert after_tool and after_tool[-1]["role"] == "user"
    assert "use the EU region" in json.dumps(after_tool[-1]["content"])
    assert [e["message_ids"] for e in events if e["type"] == "task.user_message_delivered"] == [["req_s"]]


def test_unknown_or_cancelled_runs_refuse_steers(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    assert runtime.submit_user_steer("tsk_missing", message_id="m", content="hi") is False
    assert runtime.withdraw_user_steer("tsk_missing", message_id="m") is False


def test_inbox_contract() -> None:
    inbox = SteerInbox()
    assert inbox.submit("a", "first")
    assert inbox.submit("b", "second")
    assert inbox.submit("a", "first")  # no duplicate
    assert inbox.withdraw("b") is True
    assert inbox.withdraw("b") is False
    drained = inbox.drain()
    assert [item.message_id for item in drained] == ["a"]
    assert inbox.submit("a", "first") is True  # already delivered: still no duplicate
    assert inbox.drain() == []
    assert inbox.submit("c", "third")
    leftover = inbox.close()
    assert [item.message_id for item in leftover] == ["c"]
    assert inbox.submit("d", "fourth") is False
    assert inbox.drain() == []  # closed: nothing reaches a model any more
    assert inbox.withdraw("c") is True  # withdraw still works after close
    assert inbox.submit("", "x") is False and inbox.submit("e", "   ") is False


def test_render_and_boundary_helpers() -> None:
    one = render_steers_for_model([type("S", (), {"message_id": "a", "content": "hi"})()])  # type: ignore[list-item]
    assert one.endswith("hi") and one.startswith(STEER_PREAMBLE)
    assert last_message_accepts_user_text([{"role": "user", "content": "q"}])
    assert last_message_accepts_user_text([{"role": "tool", "content": "r"}])
    assert not last_message_accepts_user_text([{"role": "assistant", "content": "partial"}])
    assert not last_message_accepts_user_text([])


def test_oversize_steer_is_refused_instead_of_truncated() -> None:
    inbox = SteerInbox()
    exact = "a" * MAX_STEER_CHARS
    oversize = "b" * (MAX_STEER_CHARS + 1)
    assert inbox.submit("exact", exact) is True
    assert inbox.submit("oversize", oversize) is False
    drained = inbox.drain()
    assert [(item.message_id, item.content) for item in drained] == [("exact", exact)]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["anthropic", "fireworks_glm"])
async def test_steer_inbox_is_closed_before_task_failed(tmp_path, provider: str) -> None:
    runtime = _runtime(tmp_path, provider=provider)
    task = _signed_task("signing-secret")
    inbox_open_at_failed: list[bool] = []
    refused_at_failed: list[bool] = []

    async def failing_anthropic(*, messages, **_kwargs):
        assert runtime.submit_user_steer(task.task_id, message_id="m_pre", content="before error") is True
        raise RuntimeError("upstream exploded")
        yield  # pragma: no cover

    async def failing_openai(**_kwargs):
        assert runtime.submit_user_steer(task.task_id, message_id="m_pre", content="before error") is True
        raise RuntimeError("upstream exploded")
        yield  # pragma: no cover

    runtime._stream_anthropic_events = failing_anthropic  # type: ignore[method-assign]
    runtime._stream_openai_chat_events = failing_openai  # type: ignore[method-assign]
    await runtime.start()
    try:
        events = []
        async for event in runtime.stream_task(task):
            events.append(event)
            if event["type"] == "task.failed":
                run_state = runtime._active_runs.get(task.task_id)  # noqa: SLF001
                inbox_open_at_failed.append(bool(run_state and run_state.steer_inbox.open))
                refused_at_failed.append(
                    runtime.submit_user_steer(task.task_id, message_id="m_post", content="too late")
                )
    finally:
        await runtime.stop()

    assert [e["type"] for e in events] == ["task.created", "task.failed"]
    assert inbox_open_at_failed == [False]
    assert refused_at_failed == [False]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["anthropic", "fireworks_glm"])
async def test_steer_inbox_is_closed_before_task_cancelled(tmp_path, provider: str) -> None:
    runtime = _runtime(tmp_path, provider=provider)
    task = _signed_task("signing-secret")
    started = asyncio.Event()
    inbox_open_at_cancelled: list[bool] = []
    refused_at_cancelled: list[bool] = []
    events: list[dict] = []

    async def hanging_anthropic(*, messages, **_kwargs):
        started.set()
        await asyncio.Future()
        yield  # pragma: no cover

    async def hanging_openai(**_kwargs):
        started.set()
        await asyncio.Future()
        yield  # pragma: no cover

    runtime._stream_anthropic_events = hanging_anthropic  # type: ignore[method-assign]
    runtime._stream_openai_chat_events = hanging_openai  # type: ignore[method-assign]
    await runtime.start()
    try:
        async def consume() -> None:
            async for event in runtime.stream_task(task):
                events.append(event)
                if event["type"] == "task.cancelled":
                    run_state = runtime._active_runs.get(task.task_id)  # noqa: SLF001
                    inbox_open_at_cancelled.append(bool(run_state and run_state.steer_inbox.open))
                    refused_at_cancelled.append(
                        runtime.submit_user_steer(task.task_id, message_id="m_post", content="too late")
                    )

        consumer = asyncio.create_task(consume())
        await started.wait()
        assert runtime.submit_user_steer(task.task_id, message_id="m_pre", content="before cancel") is True
        assert await runtime.cancel_task(task.task_id) is True
        await consumer
    finally:
        await runtime.stop()

    assert [e["type"] for e in events] == ["task.created", "task.cancelled"]
    assert inbox_open_at_cancelled == [False]
    assert refused_at_cancelled == [False]
