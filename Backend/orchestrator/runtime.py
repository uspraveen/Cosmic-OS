"""COSMIC Orchestrator Runtime — Full Agentic Loop.

Implements the core agentic cycle:
  1. Send user query + conversation context + tools to Claude Opus
  2. Stream the response (thinking + text + tool_use blocks)
  3. If stop_reason == "tool_use": execute tools, append results, loop back to 1
  4. If stop_reason == "end_turn": emit final response, done

Thinking blocks with signatures are properly tracked so the full assistant
message can be echoed back for multi-turn tool use conversations.

All events are yielded as dicts for ndjson streaming back to the Gateway.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator
from uuid import uuid4

from urllib.parse import urlparse

import httpx
import redis.asyncio as redis

from gateway.adapters.response_processor import AWAITING_REPLY_TAG
from registry import RegistryStore, find_available_instance, find_available_instance_for_agent
from shared import (
    AgentError,
    AgentResult,
    BackpressureError,
    EventEnvelope,
    SOURCE_PRIORITY_MAP,
    TaskEnvelope,
    TaskInProgress,
    create_redis_client,
    dispatch_task,
    ensure_stream_group,
    generate_task_id,
    parse_event_envelope,
    parse_stream_payload,
    sign_task_envelope,
    verify_task_envelope,
)

from .config import OrchestratorConfig
from .prompts import build_agentic_system_prompt
from .store.ledger import TaskLedger
from .tools.executor import ToolExecutionContext, ToolExecutor
from .tools.registry import (
    build_tool_progress_message,
    get_model_tool_definitions,
    get_parallel_safe_local_tool_names,
)

logger = logging.getLogger(__name__)

_PARALLEL_SAFE_TOOLS = get_parallel_safe_local_tool_names()


@dataclass(slots=True)
class SSEEvent:
    event: str
    data: str


@dataclass(slots=True)
class ContentBlock:
    """Tracks a single content block as it streams from the Anthropic API."""
    index: int
    block_type: str
    # Thinking
    thinking_text: str = ""
    signature: str = ""
    # Text
    text: str = ""
    # Tool use (client-side) and server_tool_use (server-side)
    tool_id: str = ""
    tool_name: str = ""
    input_json: str = ""
    # Raw block for opaque server-side result blocks (web_search_tool_result, web_fetch_tool_result, code_execution_tool_result)
    raw_block: dict[str, Any] | None = None

    def to_api_dict(self) -> dict[str, Any]:
        """Convert to the dict format required by the Anthropic Messages API."""
        if self.block_type == "thinking":
            result: dict[str, Any] = {"type": "thinking", "thinking": self.thinking_text}
            if self.signature:
                result["signature"] = self.signature
            return result
        if self.block_type == "text":
            return {"type": "text", "text": self.text}
        if self.block_type == "tool_use":
            try:
                parsed_input = json.loads(self.input_json) if self.input_json else {}
            except json.JSONDecodeError:
                parsed_input = {}
            return {
                "type": "tool_use",
                "id": self.tool_id,
                "name": self.tool_name,
                "input": parsed_input,
            }
        if self.block_type == "server_tool_use":
            try:
                parsed_input = json.loads(self.input_json) if self.input_json else {}
            except json.JSONDecodeError:
                parsed_input = {}
            return {
                "type": "server_tool_use",
                "id": self.tool_id,
                "name": self.tool_name,
                "input": parsed_input,
            }
        if self.block_type in ("web_search_tool_result", "web_fetch_tool_result", "code_execution_tool_result"):
            # Echo the entire raw block back — the API needs it for multi-turn
            if self.raw_block:
                return dict(self.raw_block)
            return {"type": self.block_type}
        return {"type": self.block_type}


@dataclass(slots=True)
class ActiveTaskRun:
    runner_task: asyncio.Task[Any] | None
    request_id: str | None
    session_id: str | None
    channel: str | None
    cancel_requested: bool = False
    cancel_message: str = "Response stopped."


@dataclass(slots=True)
class AnthropicLoopStats:
    anthropic_requests: int = 0
    tasks_observed: int = 0
    tasks_with_tool_loops: int = 0
    tasks_with_container_capture: int = 0
    container_reuse_turns: int = 0
    max_request_context_chars: int = 0
    max_request_message_count: int = 0
    max_tool_iterations: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "anthropic_requests": self.anthropic_requests,
            "tasks_observed": self.tasks_observed,
            "tasks_with_tool_loops": self.tasks_with_tool_loops,
            "tasks_with_container_capture": self.tasks_with_container_capture,
            "container_reuse_turns": self.container_reuse_turns,
            "max_request_context_chars": self.max_request_context_chars,
            "max_request_message_count": self.max_request_message_count,
            "max_tool_iterations": self.max_tool_iterations,
        }


@dataclass(slots=True)
class AgentDispatchStats:
    dispatches_started: int = 0
    dispatches_completed: int = 0
    dispatch_failures: int = 0
    events_consumed: int = 0
    deferred_events: int = 0
    rejected_events: int = 0
    failed_events: int = 0
    wait_timeouts: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "dispatches_started": self.dispatches_started,
            "dispatches_completed": self.dispatches_completed,
            "dispatch_failures": self.dispatch_failures,
            "events_consumed": self.events_consumed,
            "deferred_events": self.deferred_events,
            "rejected_events": self.rejected_events,
            "failed_events": self.failed_events,
            "wait_timeouts": self.wait_timeouts,
        }


class OrchestratorRuntime:
    def __init__(
        self,
        config: OrchestratorConfig,
        *,
        client: httpx.AsyncClient | None = None,
        redis_client: redis.Redis | None = None,
    ) -> None:
        self.config = config
        timeout = httpx.Timeout(config.request_timeout_sec, connect=min(config.request_timeout_sec, 15.0))
        self._client = client or httpx.AsyncClient(timeout=timeout, http2=True)
        self._owns_client = client is None
        self._redis = redis_client if redis_client is not None else (
            create_redis_client(config.redis_url) if config.redis_url else None
        )
        self._owns_redis = redis_client is None and self._redis is not None
        self.task_ledger = TaskLedger(config.task_ledger_db_path)
        self.registry_store = RegistryStore(config.agent_registry_db_path)
        self.started = False
        self._active_runs: dict[str, ActiveTaskRun] = {}
        self._pending_input_futures: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._pending_agent_results: dict[str, asyncio.Future[AgentResult | TaskInProgress]] = {}
        self._reply_consumer_task: asyncio.Task[None] | None = None
        self._agent_event_consumer_task: asyncio.Task[None] | None = None
        self._tool_executor: ToolExecutor | None = None
        self._anthropic_loop_stats = AnthropicLoopStats()
        self._agent_dispatch_stats = AgentDispatchStats()
        self._agent_event_consumer_name = f"orchestrator-events-{id(self)}"

    async def start(self) -> None:
        self.task_ledger.initialize()
        self.registry_store.initialize()
        if self._redis is not None:
            await ensure_stream_group(
                self._redis,
                stream=self.config.agent_events_stream,
                group=self.config.agent_events_group,
            )
            await ensure_stream_group(
                self._redis,
                stream=self.config.task_input_replies_stream,
                group=self.config.task_input_orchestrator_group,
            )
            self._agent_event_consumer_task = asyncio.create_task(
                self._agent_event_consumer_loop(),
                name=self._agent_event_consumer_name,
            )
            self._reply_consumer_task = asyncio.create_task(
                self._user_reply_consumer_loop(),
                name="orchestrator-user-reply-consumer",
            )

        self._tool_executor = ToolExecutor(
            perplexity_api_key=self.config.perplexity_api_key,
            perplexity_model=self.config.perplexity_model,
            cosmic_memory_url=self.config.cosmic_memory_url,
            gateway_url=self.config.gateway_url,
            gateway_internal_token=self.config.internal_token,
        )
        self.started = True

    async def stop(self) -> None:
        if self._agent_event_consumer_task is not None:
            self._agent_event_consumer_task.cancel()
            await asyncio.gather(self._agent_event_consumer_task, return_exceptions=True)
            self._agent_event_consumer_task = None
        if self._reply_consumer_task is not None:
            self._reply_consumer_task.cancel()
            await asyncio.gather(self._reply_consumer_task, return_exceptions=True)
            self._reply_consumer_task = None
        for future in list(self._pending_input_futures.values()):
            if not future.done():
                future.cancel()
        self._pending_input_futures.clear()
        for future in list(self._pending_agent_results.values()):
            if not future.done():
                future.cancel()
        self._pending_agent_results.clear()
        if self._tool_executor is not None:
            await self._tool_executor.close()
            self._tool_executor = None
        if self._owns_client:
            await self._client.aclose()
        if self._owns_redis and self._redis is not None:
            await self._redis.aclose()
        self.started = False

    # ════════════════════════════════════════════════════════════
    #  AGENTIC LOOP
    # ════════════════════════════════════════════════════════════

    async def stream_task(self, task: TaskEnvelope) -> AsyncIterator[dict[str, Any]]:
        if not verify_task_envelope(task, self.config.signing_secret):
            raise RuntimeError("TaskEnvelope signature verification failed.")
        if not self.config.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured in orchestrator.env.")

        request_id = str(task.input.get("request_id") or "").strip() or None
        query = str(task.input.get("query") or "").strip()
        session_id = task.session_id
        channel = task.channel
        if not query:
            raise RuntimeError("TaskEnvelope.input.query is required for orchestrator.process")

        self.task_ledger.create_task(task)
        self._active_runs[task.task_id] = ActiveTaskRun(
            runner_task=asyncio.current_task(),
            request_id=request_id,
            session_id=session_id,
            channel=channel,
        )

        ev = {
            "task_id": task.task_id,
            "request_id": request_id,
            "session_id": session_id,
            "channel": channel,
        }

        yield {**ev, "type": "task.created", "route": "opus", "status": "running"}

        started_at = time.perf_counter()
        cumulative_usage: dict[str, int] = {}
        stop_reason: str | None = None
        tool_context = ToolExecutionContext(
            task_id=task.task_id,
            request_id=request_id,
            session_id=session_id,
            channel=channel,
            source=task.source,
            source_id=task.source_id,
        )

        try:
            messages = self._build_messages(task)
            system_prompt = build_agentic_system_prompt(
                str(task.input.get("memory_context") or "").strip() or None,
                user_timezone=str(task.input.get("user_timezone") or "").strip() or None,
            )
            tools = get_model_tool_definitions()
            max_iterations = self.config.max_tool_iterations

            iteration = 0
            full_response_text = ""
            full_reasoning_text = ""
            collected_sources: list[dict[str, str]] = []
            container_id: str | None = None
            container_captured = False
            container_reuse_turns = 0
            anthropic_requests = 0
            max_request_context_chars = 0
            max_request_message_count = 0
            saw_tool_loop = False

            while iteration < max_iterations:
                iteration += 1
                anthropic_requests += 1
                if container_id:
                    container_reuse_turns += 1
                max_request_context_chars = max(
                    max_request_context_chars,
                    self._estimate_request_context_chars(system_prompt, messages),
                )
                max_request_message_count = max(max_request_message_count, len(messages))

                # ── Stream one Anthropic turn ───────────────────
                blocks: dict[int, ContentBlock] = {}
                turn_usage: dict[str, Any] = {}
                turn_stop_reason: str | None = None
                reasoning_announced = False
                responding_announced = False

                async for sse in self._stream_anthropic_events(
                    system_prompt=system_prompt,
                    messages=messages,
                    tools=tools,
                    container_id=container_id,
                ):
                    if sse.event == "ping" or not sse.data:
                        continue
                    payload = json.loads(sse.data)
                    ptype = str(payload.get("type") or "")

                    # ── message_start ───────────────────────────
                    if ptype == "message_start":
                        msg_obj = payload.get("message", {})
                        turn_usage = self._merge_usage(turn_usage, msg_obj.get("usage"))
                        # Fallback container capture from message_start
                        _cont = msg_obj.get("container")
                        if isinstance(_cont, dict):
                            _cid = _cont.get("id")
                            if _cid:
                                container_id = str(_cid)
                                container_captured = True
                        continue

                    # ── message_delta ───────────────────────────
                    if ptype == "message_delta":
                        turn_usage = self._merge_usage(turn_usage, payload.get("usage"))
                        delta = payload.get("delta")
                        if isinstance(delta, dict):
                            turn_stop_reason = str(delta.get("stop_reason") or "").strip() or turn_stop_reason
                            # Capture container_id from delta.container.id (primary location)
                            _cont = delta.get("container")
                            if isinstance(_cont, dict):
                                _cid = _cont.get("id")
                                if _cid:
                                    container_id = str(_cid)
                                    container_captured = True
                        continue

                    # ── error ───────────────────────────────────
                    if ptype == "error":
                        err = payload.get("error")
                        msg = str(err.get("message") or "Anthropic stream error") if isinstance(err, dict) else "Anthropic stream error"
                        raise RuntimeError(msg)

                    # ── content_block_start ─────────────────────
                    if ptype == "content_block_start":
                        idx = int(payload.get("index", 0))
                        cb = payload.get("content_block") or {}
                        btype = str(cb.get("type") or "text")
                        block = ContentBlock(index=idx, block_type=btype)
                        if btype == "tool_use":
                            block.tool_id = str(cb.get("id") or "")
                            block.tool_name = str(cb.get("name") or "")
                        elif btype == "server_tool_use":
                            block.tool_id = str(cb.get("id") or "")
                            block.tool_name = str(cb.get("name") or "")
                        elif btype in ("web_search_tool_result", "web_fetch_tool_result", "code_execution_tool_result"):
                            block.raw_block = dict(cb)
                        blocks[idx] = block
                        # Emit progress for server-side tool calls
                        if btype == "server_tool_use":
                            progress_msg = "Searching the web..." if block.tool_name == "web_search" else "Fetching web page..."
                            yield {**ev, "type": "task.progress", "status": "tool_call", "iteration": iteration, "tool_name": block.tool_name, "message": progress_msg}
                        continue

                    # ── content_block_delta ─────────────────────
                    if ptype == "content_block_delta":
                        idx = int(payload.get("index", 0))
                        block = blocks.get(idx)
                        if block is None:
                            continue
                        delta = payload.get("delta") or {}
                        dtype = str(delta.get("type") or "")

                        if dtype == "thinking_delta":
                            chunk = str(delta.get("thinking") or "")
                            if not chunk:
                                continue
                            block.thinking_text += chunk
                            if not reasoning_announced:
                                reasoning_announced = True
                                yield {**ev, "type": "task.progress", "status": "thinking", "message": "Opus is reasoning through the request."}
                            if iteration == 1:
                                yield {**ev, "type": "response.thinking.chunk", "content": chunk, "done": False}

                        elif dtype == "signature_delta":
                            sig = str(delta.get("signature") or "")
                            block.signature += sig

                        elif dtype == "text_delta":
                            chunk = str(delta.get("text") or "")
                            if not chunk:
                                continue
                            block.text += chunk
                            if not responding_announced:
                                responding_announced = True
                                yield {**ev, "type": "task.progress", "status": "responding", "message": "Opus is writing the response."}
                            yield {**ev, "type": "response.chunk", "content": chunk, "done": False}

                        elif dtype == "input_json_delta":
                            partial = str(delta.get("partial_json") or "")
                            block.input_json += partial

                        continue

                    # ── content_block_stop ─────────────────────
                    if ptype == "content_block_stop":
                        idx = int(payload.get("index", 0))
                        block = blocks.get(idx)
                        if block and block.block_type == "server_tool_use":
                            # Now we have the full input — emit detailed progress
                            try:
                                pi = json.loads(block.input_json) if block.input_json else {}
                            except json.JSONDecodeError:
                                pi = {}
                            progress_msg = build_tool_progress_message(block.tool_name, pi)
                            yield {
                                **ev, "type": "task.progress",
                                "status": "tool_call",
                                "iteration": iteration,
                                "tool_name": block.tool_name,
                                "message": progress_msg,
                            }
                        continue
                    # message_stop — nothing to do

                # ── End of Anthropic turn ───────────────────────
                cumulative_usage = self._merge_usage(cumulative_usage, turn_usage)
                stop_reason = turn_stop_reason

                # Collect text and reasoning from this turn
                turn_text_parts: list[str] = []
                turn_reasoning_parts: list[str] = []
                turn_tool_blocks: list[ContentBlock] = []
                turn_server_blocks: list[ContentBlock] = []
                for idx in sorted(blocks):
                    b = blocks[idx]
                    if b.block_type == "thinking" and b.thinking_text:
                        turn_reasoning_parts.append(b.thinking_text)
                    elif b.block_type == "text" and b.text:
                        turn_text_parts.append(b.text)
                    elif b.block_type == "tool_use":
                        turn_tool_blocks.append(b)
                    elif b.block_type in (
                        "server_tool_use",
                        "web_search_tool_result",
                        "web_fetch_tool_result",
                        "code_execution_tool_result",
                    ):
                        turn_server_blocks.append(b)
                        if b.block_type == "web_search_tool_result" and b.raw_block:
                            self._collect_native_search_sources(b.raw_block, collected_sources)

                turn_text = "".join(turn_text_parts)
                turn_reasoning = "".join(turn_reasoning_parts)
                full_response_text += turn_text
                full_reasoning_text += turn_reasoning

                # ── Server-side tool continuation (pause_turn) ────
                if turn_stop_reason == "pause_turn":
                    saw_tool_loop = True
                    assistant_content = [blocks[idx].to_api_dict() for idx in sorted(blocks)]
                    messages.append({"role": "assistant", "content": assistant_content})
                    yield {
                        **ev, "type": "task.progress",
                        "status": "tool_loop",
                        "iteration": iteration,
                        "message": self._build_server_tool_loop_message(turn_server_blocks),
                    }
                    continue

                # ── Tool use → execute and loop ─────────────────
                if turn_stop_reason == "tool_use" and turn_tool_blocks:
                    saw_tool_loop = True
                    # Reconstruct the full assistant message (thinking + text + tool_use)
                    assistant_content = [blocks[idx].to_api_dict() for idx in sorted(blocks)]
                    messages.append({"role": "assistant", "content": assistant_content})

                    # Parse inputs and emit progress events for all tool calls
                    parsed_inputs: list[dict[str, Any]] = []
                    for tb in turn_tool_blocks:
                        try:
                            pi = json.loads(tb.input_json) if tb.input_json else {}
                        except json.JSONDecodeError:
                            pi = {}
                        parsed_inputs.append(pi)

                        progress_msg = build_tool_progress_message(tb.tool_name, pi)
                        yield {
                            **ev, "type": "task.progress",
                            "status": "tool_call",
                            "iteration": iteration,
                            "tool_name": tb.tool_name,
                            "message": progress_msg,
                        }
                        yield {
                            **ev, "type": "tool.call",
                            "iteration": iteration,
                            "tool_name": tb.tool_name,
                            "tool_call_id": tb.tool_id,
                            "tool_input": pi,
                        }

                    # Check cancellation before executing
                    run_state = self._active_runs.get(task.task_id)
                    if run_state and run_state.cancel_requested:
                        raise asyncio.CancelledError()

                    assert self._tool_executor is not None

                    # Execute tools — parallel for read-only, sequential for side-effect tools
                    all_read_only = all(tb.tool_name in _PARALLEL_SAFE_TOOLS for tb in turn_tool_blocks)
                    result_strs: list[str] = []

                    if all_read_only and len(turn_tool_blocks) > 1:
                        # All tools are read-only → run concurrently
                        result_strs = list(await asyncio.gather(*(
                            self._tool_executor.execute(tb.tool_name, pi, context=tool_context)
                            for tb, pi in zip(turn_tool_blocks, parsed_inputs)
                        )))
                    else:
                        # Mixed or single tool → run sequentially
                        for tb, pi in zip(turn_tool_blocks, parsed_inputs):
                            result_strs.append(await self._tool_executor.execute(tb.tool_name, pi, context=tool_context))

                    # Collect results and emit tool.result events
                    tool_results: list[dict[str, Any]] = []
                    for tb, pi, result_str in zip(turn_tool_blocks, parsed_inputs, result_strs):
                        if tb.tool_name == "perplexity_research":
                            self._collect_perplexity_sources(result_str, collected_sources)

                        yield {
                            **ev, "type": "tool.result",
                            "iteration": iteration,
                            "tool_name": tb.tool_name,
                            "tool_call_id": tb.tool_id,
                            "result_preview": result_str[:500],
                        }
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tb.tool_id,
                            "content": result_str,
                        })

                    messages.append({"role": "user", "content": tool_results})

                    yield {
                        **ev, "type": "task.progress",
                        "status": "tool_loop",
                        "iteration": iteration,
                        "tools_called": [tb.tool_name for tb in turn_tool_blocks],
                        "message": self._build_local_tool_loop_message(
                            turn_tool_blocks,
                            parsed_inputs,
                            result_strs,
                            parallel=all_read_only and len(turn_tool_blocks) > 1,
                        ),
                    }
                    continue

                # ── Final response (end_turn or other) ──────────
                break

            # ── Emit completion ─────────────────────────────────
            hit_max_iterations = iteration >= max_iterations and stop_reason in ("tool_use", "pause_turn")
            result_type = "max_iterations" if hit_max_iterations else "success"

            display_text = full_response_text.rstrip()
            awaiting_reply = display_text.endswith(AWAITING_REPLY_TAG)
            if awaiting_reply:
                display_text = display_text.removesuffix(AWAITING_REPLY_TAG).rstrip()

            result_payload = {
                "content": display_text,
                "thinking_text": full_reasoning_text,
                "awaiting_reply": awaiting_reply,
                "usage": cumulative_usage,
                "stop_reason": stop_reason,
                "result_type": result_type,
                "tool_iterations": iteration,
                "loop_diagnostics": {
                    "anthropic_requests": anthropic_requests,
                    "container_captured": container_captured,
                    "container_reuse_turns": container_reuse_turns,
                    "max_request_context_chars": max_request_context_chars,
                    "max_request_message_count": max_request_message_count,
                },
            }
            self.task_ledger.mark_completed(task.task_id, result=result_payload)
            elapsed_ms = max(1, int((time.perf_counter() - started_at) * 1000))

            complete_event: dict[str, Any] = {
                **ev,
                "type": "response.complete",
                "content": display_text,
                "route": "opus",
                "result_type": result_type,
                "awaiting_reply": awaiting_reply,
                "thinking_text": full_reasoning_text,
                "metrics": {
                    "rtt_ms": elapsed_ms,
                    "tool_iterations": iteration,
                    "anthropic_requests": anthropic_requests,
                    "container_captured": container_captured,
                    "container_reuse_turns": container_reuse_turns,
                    "max_request_context_chars": max_request_context_chars,
                    "max_request_message_count": max_request_message_count,
                    **cumulative_usage,
                },
            }
            if collected_sources:
                complete_event["sources"] = collected_sources
            yield complete_event
            yield {**ev, "type": "task.completed", "route": "opus", "status": "completed"}

        except asyncio.CancelledError:
            run_state = self._active_runs.get(task.task_id)
            if run_state and run_state.cancel_requested:
                message = run_state.cancel_message
                self.task_ledger.mark_cancelled(task.task_id, message=message)
                yield {**ev, "type": "task.cancelled", "route": "opus", "status": "cancelled", "message": message}
                return
            raise
        except Exception as exc:
            message = str(exc).strip() or "Orchestrator processing failed."
            self.task_ledger.mark_failed(task.task_id, code="OPUS_UPSTREAM_ERROR", message=message)
            yield {
                **ev, "type": "task.failed", "route": "opus", "status": "failed",
                "error": {"code": "OPUS_UPSTREAM_ERROR", "message": message, "retryable": False},
            }
        finally:
            self._record_anthropic_loop_stats(
                anthropic_requests=locals().get("anthropic_requests", 0),
                saw_tool_loop=locals().get("saw_tool_loop", False),
                container_captured=locals().get("container_captured", False),
                container_reuse_turns=locals().get("container_reuse_turns", 0),
                max_request_context_chars=locals().get("max_request_context_chars", 0),
                max_request_message_count=locals().get("max_request_message_count", 0),
                tool_iterations=locals().get("iteration", 0),
            )
            self._active_runs.pop(task.task_id, None)

    # ════════════════════════════════════════════════════════════
    #  Task management
    # ════════════════════════════════════════════════════════════

    def list_active_tasks(self, *, session_id: str | None = None, channel: str | None = None) -> list[dict[str, Any]]:
        return self.task_ledger.list_active_tasks(session_id=session_id, channel=channel)

    def cancel_task(self, task_id: str, *, message: str = "Response stopped.") -> bool:
        tid = str(task_id or "").strip()
        if not tid:
            return False
        run_state = self._active_runs.get(tid)
        if run_state is None:
            return False
        run_state.cancel_requested = True
        run_state.cancel_message = message
        runner = run_state.runner_task
        if runner is not None and not runner.done():
            runner.cancel()
        return True

    def get_loop_diagnostics_snapshot(self) -> dict[str, int]:
        return self._anthropic_loop_stats.as_dict()

    async def list_registered_agents(self) -> list[dict[str, Any]]:
        rows = self.registry_store.list_agents(status=None)
        results: list[dict[str, Any]] = []
        for row in rows:
            agent_id = str(row.get("agent_id") or "").strip()
            intents = [item["intent"] for item in self.registry_store.list_intents(agent_id) if item.get("intent")]
            healthy_instance = False
            instance_id: str | None = None
            if self._redis is not None and agent_id:
                found_agent_id, found_instance_id = await find_available_instance_for_agent(agent_id, self._redis)
                healthy_instance = bool(found_agent_id and found_instance_id)
                instance_id = found_instance_id
            results.append(
                {
                    **row,
                    "intents": intents,
                    "healthy_instance": healthy_instance,
                    "instance_id": instance_id,
                }
            )
        return results

    async def get_agent_dispatch_snapshot(self) -> dict[str, Any]:
        agents = await self.list_registered_agents()
        return {
            "enabled": self._redis is not None,
            "registry_db_path": str(self.config.agent_registry_db_path),
            "events_stream": self.config.agent_events_stream,
            "events_group": self.config.agent_events_group,
            "consumer_running": self._agent_event_consumer_task is not None and not self._agent_event_consumer_task.done(),
            "consumer_name": self._agent_event_consumer_name if self._redis is not None else None,
            "registered_agents": len(agents),
            "healthy_agents": sum(1 for item in agents if item.get("healthy_instance")),
            "pending_results": len(self._pending_agent_results),
            "stats": self._agent_dispatch_stats.as_dict(),
            "agents": agents,
        }

    async def dispatch_agent_task(
        self,
        *,
        parent_task: TaskEnvelope,
        intent: str,
        input_payload: dict[str, Any] | None = None,
        agent_id: str | None = None,
        priority: str | None = None,
        idempotency_key: str | None = None,
        wait_timeout_sec: float | None = None,
    ) -> AgentResult | TaskInProgress:
        if self._redis is None:
            raise RuntimeError("REDIS_URL is not configured in orchestrator.env.")

        resolved_intent = str(intent or "").strip()
        if not resolved_intent:
            raise RuntimeError("intent is required for agent dispatch.")

        candidate = await self._find_available_agent(resolved_intent, preferred_agent_id=agent_id)
        recipient = str(candidate["agent_id"])
        timeout_sec = max(1, int(candidate.get("timeout_sec") or self.config.request_timeout_sec))
        child_deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_sec)
        if parent_task.deadline_ts is not None and parent_task.deadline_ts < child_deadline:
            child_deadline = parent_task.deadline_ts
        child_input = dict(input_payload or {})
        if "request_id" not in child_input:
            inherited_request_id = str(parent_task.input.get("request_id") or "").strip()
            if inherited_request_id:
                child_input["request_id"] = inherited_request_id

        child_priority = str(priority or parent_task.priority or SOURCE_PRIORITY_MAP.get(parent_task.source, "normal")).strip()
        normalized_idempotency_key = str(idempotency_key or "").strip() or self._build_child_idempotency_key(
            parent_task.idempotency_key,
            recipient,
            resolved_intent,
            child_input,
        )

        child_task = TaskEnvelope(
            task_id=generate_task_id(),
            task_list_id=parent_task.task_list_id,
            parent_task_id=parent_task.task_id,
            session_id=parent_task.session_id,
            sender=self.config.orchestrator_agent_id,
            recipient=recipient,
            intent=resolved_intent,
            input=child_input,
            input_artifacts=[],
            idempotency_key=normalized_idempotency_key,
            deadline_ts=child_deadline,
            priority=child_priority if child_priority in {"high", "normal", "low"} else "normal",
            leader_epoch=None,
            signature="",
            source=parent_task.source,
            source_id=parent_task.source_id,
            channel=parent_task.channel,
        )
        signature = sign_task_envelope(child_task, self._resolve_agent_secret(recipient))
        child_task = child_task.model_copy(update={"signature": signature})
        self.task_ledger.create_task(child_task)

        wait_timeout = wait_timeout_sec if wait_timeout_sec is not None else float(timeout_sec)
        pending_result: asyncio.Future[AgentResult | TaskInProgress] | None = None
        if wait_timeout > 0:
            pending_result = asyncio.get_running_loop().create_future()
            self._pending_agent_results[child_task.task_id] = pending_result

        try:
            await dispatch_task(child_task, self._redis)
            self._agent_dispatch_stats.dispatches_started += 1

            if pending_result is None:
                result = self._build_in_progress_result(child_task.task_id, normalized_idempotency_key, timeout_sec=timeout_sec)
                self.task_ledger.mark_deferred(child_task.task_id, result=result.model_dump(mode="json"))
                return result

            try:
                return await asyncio.wait_for(asyncio.shield(pending_result), timeout=wait_timeout)
            except asyncio.TimeoutError:
                self._agent_dispatch_stats.wait_timeouts += 1
                result = self._build_in_progress_result(child_task.task_id, normalized_idempotency_key, timeout_sec=timeout_sec)
                self.task_ledger.mark_deferred(child_task.task_id, result=result.model_dump(mode="json"))
                return result
        except BackpressureError as exc:
            self._agent_dispatch_stats.dispatch_failures += 1
            self.task_ledger.mark_failed(child_task.task_id, code="BACKPRESSURE", message=str(exc))
            raise RuntimeError(str(exc)) from exc
        except Exception as exc:
            self._agent_dispatch_stats.dispatch_failures += 1
            message = str(exc).strip()[:500] or "Agent dispatch failed."
            self.task_ledger.mark_failed(child_task.task_id, code="DISPATCH_ERROR", message=message)
            raise
        finally:
            if pending_result is None or pending_result.done():
                self._pending_agent_results.pop(child_task.task_id, None)

    async def request_user_input(
        self,
        task_id: str,
        *,
        question: str,
        options: list[str] | None = None,
        channel: str | None = None,
        agent: str = "cosmic/orchestrator:1.0.0",
        wait_timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        if self._redis is None:
            raise RuntimeError("REDIS_URL is not configured in orchestrator.env.")
        ntid = str(task_id or "").strip()
        nq = str(question or "").strip()
        if not ntid or not nq:
            raise RuntimeError("task_id and question are required for task input requests.")

        run_state = self._active_runs.get(ntid)
        resolved_channel = str(channel or (run_state.channel if run_state else "") or "").strip() or None
        resolved_session = run_state.session_id if run_state else None
        irid = f"uir_{uuid4().hex[:12]}"
        payload = {
            "input_request_id": irid,
            "task_id": ntid,
            "session_id": resolved_session,
            "agent": agent,
            "channel": resolved_channel,
            "question": nq,
            "options": [str(i) for i in options or [] if str(i).strip()],
            "status": "pending",
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        self.task_ledger.create_task_input_request(
            input_request_id=irid, task_id=ntid, session_id=resolved_session,
            channel=resolved_channel, agent=agent, question=nq, options=payload["options"],
        )
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_input_futures[irid] = future
        try:
            await self._redis.xadd(
                self.config.task_input_requests_stream,
                {"payload": json.dumps(payload, ensure_ascii=False)},
            )
            if wait_timeout_sec is None or wait_timeout_sec <= 0:
                return payload
            try:
                reply = await asyncio.wait_for(asyncio.shield(future), timeout=wait_timeout_sec)
            except asyncio.TimeoutError:
                return payload
            return {**payload, "reply": reply, "status": "answered"}
        finally:
            if future.done() or (wait_timeout_sec is None or wait_timeout_sec <= 0):
                self._pending_input_futures.pop(irid, None)

    def _resolve_agent_secret(self, agent_id: str) -> str:
        normalized_agent_id = str(agent_id or "").strip()
        secret = (
            self.config.agent_signing_secrets.get(normalized_agent_id)
            or self.config.signing_secret
        ).strip()
        if not secret:
            raise RuntimeError(f"No signing secret configured for agent {normalized_agent_id}.")
        return secret

    def _build_child_idempotency_key(
        self,
        parent_idempotency_key: str,
        agent_id: str,
        intent: str,
        input_payload: dict[str, Any],
    ) -> str:
        fingerprint_payload = dict(input_payload)
        fingerprint_payload.pop("auth", None)
        digest = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()[:16]
        return f"{parent_idempotency_key}:{agent_id}:{intent}:{digest}"

    async def _find_available_agent(
        self,
        intent: str,
        *,
        preferred_agent_id: str | None = None,
    ) -> dict[str, Any]:
        if self._redis is None:
            raise RuntimeError("REDIS_URL is not configured in orchestrator.env.")

        registered_matches = self.registry_store.list_agents_for_intent(intent)
        if not registered_matches:
            raise RuntimeError(f"No registered agent advertises intent {intent!r}.")

        if preferred_agent_id:
            normalized_agent_id = str(preferred_agent_id or "").strip()
            match = next((item for item in registered_matches if item.get("agent_id") == normalized_agent_id), None)
            if match is None:
                if self.registry_store.get_card(normalized_agent_id) is None:
                    raise RuntimeError(f"Agent {normalized_agent_id!r} is not registered.")
                raise RuntimeError(f"Agent {normalized_agent_id!r} does not advertise intent {intent!r}.")
            found_agent_id, instance_id = await find_available_instance_for_agent(normalized_agent_id, self._redis)
            if not found_agent_id or not instance_id:
                raise RuntimeError(f"Agent {normalized_agent_id!r} is registered but has no healthy instance.")
            return {**match, "instance_id": instance_id}

        found_agent_id, instance_id = await find_available_instance(intent, self._redis)
        if not found_agent_id or not instance_id:
            raise RuntimeError(f"No healthy agent instance is available for intent {intent!r}.")

        match = next((item for item in registered_matches if item.get("agent_id") == found_agent_id), None)
        if match is None:
            raise RuntimeError(f"Healthy agent {found_agent_id!r} is not registered for intent {intent!r}.")
        return {**match, "instance_id": instance_id}

    def _build_in_progress_result(self, task_id: str, idempotency_key: str, *, timeout_sec: int) -> TaskInProgress:
        return TaskInProgress(
            task_id=task_id,
            idempotency_key=idempotency_key,
            executing_since=datetime.now(timezone.utc),
            check_after_sec=max(5, min(60, max(1, timeout_sec) // 4 or 5)),
        )

    def _coerce_agent_result(self, event: EventEnvelope) -> AgentResult:
        try:
            result = AgentResult.model_validate(event.payload)
        except Exception as exc:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="INVALID_EVENT_PAYLOAD",
                    retryable=False,
                    message=f"Invalid {event.event_type} payload: {exc}",
                    next_action="escalate",
                ),
            )

        if result.status == "failed" and result.error is None:
            return result.model_copy(
                update={
                    "error": AgentError(
                        code="AGENT_FAILED",
                        retryable=False,
                        message="Agent reported failure without an error payload.",
                        next_action="escalate",
                    )
                }
            )
        return result

    def _rejected_agent_result(self, event: EventEnvelope) -> AgentResult:
        reason = str(event.payload.get("reason") or "agent_rejected").strip().replace("_", " ")
        sender = str(event.payload.get("sender") or "").strip()
        message = f"Agent rejected dispatched task: {reason}."
        if sender:
            message = f"{message.rstrip('.')} Sender: {sender}."
        return AgentResult(
            status="failed",
            output={},
            artifacts=[],
            error=AgentError(
                code="TASK_REJECTED",
                retryable=False,
                message=message,
                next_action="escalate",
            ),
        )

    def _resolve_pending_agent_result(self, task_id: str, result: AgentResult | TaskInProgress) -> None:
        future = self._pending_agent_results.pop(task_id, None)
        if future is not None and not future.done():
            future.set_result(result)

    async def _agent_event_consumer_loop(self) -> None:
        assert self._redis is not None
        while True:
            entries = await self._redis.xreadgroup(
                groupname=self.config.agent_events_group,
                consumername=self._agent_event_consumer_name,
                streams={self.config.agent_events_stream: ">"},
                count=20,
                block=1000,
            )
            for _stream, messages in entries:
                for message_id, data in messages:
                    try:
                        event = parse_event_envelope(data)
                        await self._handle_agent_event(event)
                    except Exception as exc:
                        logger.warning("orchestrator.agent_event_invalid message_id=%s error=%s", message_id, exc)
                    finally:
                        await self._redis.xack(
                            self.config.agent_events_stream,
                            self.config.agent_events_group,
                            message_id,
                        )

    async def _handle_agent_event(self, event: EventEnvelope) -> None:
        self._agent_dispatch_stats.events_consumed += 1

        if event.event_type == "task.completed":
            result = self._coerce_agent_result(event)
            self.task_ledger.mark_completed(event.task_id, result=result.model_dump(mode="json"))
            self._agent_dispatch_stats.dispatches_completed += 1
            self._resolve_pending_agent_result(event.task_id, result)
            return

        if event.event_type in {"task.failed", "task.dlq"}:
            result = self._coerce_agent_result(event)
            error = result.error or AgentError(
                code="AGENT_FAILED",
                retryable=False,
                message=f"Agent emitted {event.event_type}.",
                next_action="escalate",
            )
            self.task_ledger.mark_failed(event.task_id, code=error.code, message=error.message)
            self._agent_dispatch_stats.failed_events += 1
            self._resolve_pending_agent_result(event.task_id, result)
            return

        if event.event_type == "task.deferred":
            try:
                result = TaskInProgress.model_validate(event.payload)
            except Exception as exc:
                result = self._build_in_progress_result(
                    event.task_id,
                    idempotency_key=f"deferred:{event.task_id}",
                    timeout_sec=30,
                )
                logger.warning("orchestrator.agent_event_invalid_deferred task_id=%s error=%s", event.task_id, exc)
            self.task_ledger.mark_deferred(event.task_id, result=result.model_dump(mode="json"))
            self._agent_dispatch_stats.deferred_events += 1
            self._resolve_pending_agent_result(event.task_id, result)
            return

        if event.event_type == "task.rejected":
            # Current workers use task.rejected for hard dispatch rejection.
            # When epoch-based redrive lands, this branch can become redispatch-aware.
            result = self._rejected_agent_result(event)
            error = result.error
            assert error is not None
            self.task_ledger.mark_failed(event.task_id, code=error.code, message=error.message)
            self._agent_dispatch_stats.rejected_events += 1
            self._resolve_pending_agent_result(event.task_id, result)
            return

    # ════════════════════════════════════════════════════════════
    #  Anthropic API streaming
    # ════════════════════════════════════════════════════════════

    async def _stream_anthropic_events(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        container_id: str | None = None,
    ) -> AsyncIterator[SSEEvent]:
        url = "https://api.anthropic.com/v1/messages"
        system_payload: str | list[dict[str, Any]]
        if self.config.anthropic_prompt_cache_enabled:
            system_payload = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            system_payload = system_prompt
        body: dict[str, Any] = {
            "model": self.config.anthropic_model,
            "max_tokens": self.config.max_tokens,
            "stream": True,
            "thinking": {"type": "adaptive"},
            "system": system_payload,
            "messages": messages,
        }
        if tools:
            body["tools"] = tools
        if container_id:
            body["container"] = container_id

        headers = {
            "x-api-key": self.config.anthropic_api_key,
            "anthropic-version": self.config.anthropic_version,
            "content-type": "application/json",
            "accept": "text/event-stream",
        }

        for attempt in range(3):
            yielded_any = False
            try:
                async with self._client.stream("POST", url, headers=headers, json=body) as resp:
                    if resp.status_code >= 400:
                        raw = await resp.aread()
                        raise RuntimeError(self._error_from_response(raw, resp.status_code))
                    async for item in self._iter_sse(resp):
                        yielded_any = True
                        yield item
                return
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                if yielded_any or attempt == 2:
                    raise RuntimeError(f"Anthropic API error: {exc}") from exc
                await asyncio.sleep(0.5 * (2 ** attempt))

    async def _iter_sse(self, response: httpx.Response) -> AsyncIterator[SSEEvent]:
        event_name = "message"
        data_lines: list[str] = []
        async for raw_line in response.aiter_lines():
            line = raw_line.rstrip("\r")
            if not line:
                if data_lines:
                    yield SSEEvent(event=event_name, data="\n".join(data_lines))
                event_name = "message"
                data_lines = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip() or "message"
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if data_lines:
            yield SSEEvent(event=event_name, data="\n".join(data_lines))

    def _build_messages(self, task: TaskEnvelope) -> list[dict[str, Any]]:
        """Build the messages list for the Anthropic API from conversation context + current query.

        Unlike the direct adapters which receive history that already includes the
        current user message, the orchestrator receives conversation_context
        (prior turns only) + a separate query field.  We must NOT strip trailing
        assistant messages — doing so would create consecutive user messages when
        the current query is appended, causing the model to answer multiple
        questions at once.
        """
        raw_context = task.input.get("conversation_context")
        context = raw_context if isinstance(raw_context, list) else []

        # ── Normalize prior history (keep trailing assistant!) ────
        messages: list[dict[str, Any]] = []
        for item in context:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            content = str(item.get("content") or "").strip()
            if role not in {"user", "assistant"} or not content:
                continue
            # Collapse consecutive same-role messages (safety)
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += "\n\n" + content
                continue
            messages.append({"role": role, "content": content})

        # Strip leading assistant messages (API requires first message is user)
        while messages and messages[0]["role"] != "user":
            messages.pop(0)

        # ── Build the current user query ──────────────────────────
        user_query = str(task.input.get("query") or "").strip()
        input_artifacts = task.input_artifacts if isinstance(task.input_artifacts, list) else []
        if input_artifacts:
            manifest_lines = [
                "The user attached media/artifacts with metadata below.",
                "You have metadata and references only. Do not claim to have directly viewed or listened to the bytes unless a tool actually loads them.",
                "", "Attachment manifest:",
            ]
            for i, art in enumerate(input_artifacts, 1):
                if not isinstance(art, dict):
                    continue
                parts = [
                    f"kind={str(art.get('kind') or 'unknown').strip()}",
                    f"mime={str(art.get('mime') or 'application/octet-stream').strip()}",
                ]
                for key in ("filename", "caption", "bridge_media_ref", "download_url"):
                    v = str(art.get(key) or "").strip()
                    if v:
                        parts.append(f"{key}={v}")
                sb = art.get("size_bytes")
                if sb:
                    parts.append(f"size_bytes={sb}")
                manifest_lines.append(f"{i}. " + "; ".join(parts))
            user_query = user_query + "\n\n" + "\n".join(manifest_lines) if user_query else "\n".join(manifest_lines)

        # Append the current query — if context somehow ends with user (e.g.
        # a response was never stored), collapse to avoid consecutive user turns.
        if messages and messages[-1]["role"] == "user":
            messages[-1]["content"] += "\n\n" + user_query
        else:
            messages.append({"role": "user", "content": user_query})

        return messages

    def _merge_usage(self, existing: dict[str, Any], usage: Any) -> dict[str, Any]:
        if not isinstance(usage, dict):
            return existing
        merged = dict(existing)
        for key, value in usage.items():
            if isinstance(value, (int, float)):
                merged[key] = merged.get(key, 0) + int(value)
        return merged

    def _estimate_request_context_chars(self, system_prompt: str, messages: list[dict[str, Any]]) -> int:
        try:
            messages_json = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            messages_json = repr(messages)
        return len(system_prompt) + len(messages_json)

    def _record_anthropic_loop_stats(
        self,
        *,
        anthropic_requests: int,
        saw_tool_loop: bool,
        container_captured: bool,
        container_reuse_turns: int,
        max_request_context_chars: int,
        max_request_message_count: int,
        tool_iterations: int,
    ) -> None:
        stats = self._anthropic_loop_stats
        stats.tasks_observed += 1
        stats.anthropic_requests += max(0, int(anthropic_requests))
        if saw_tool_loop:
            stats.tasks_with_tool_loops += 1
        if container_captured:
            stats.tasks_with_container_capture += 1
        stats.container_reuse_turns += max(0, int(container_reuse_turns))
        stats.max_request_context_chars = max(stats.max_request_context_chars, int(max_request_context_chars))
        stats.max_request_message_count = max(stats.max_request_message_count, int(max_request_message_count))
        stats.max_tool_iterations = max(stats.max_tool_iterations, int(tool_iterations))

    def _error_from_response(self, body: bytes, status_code: int) -> str:
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return f"status={status_code}"
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                msg = str(error.get("message") or "").strip()
                if msg:
                    return msg
        return f"status={status_code}"

    @staticmethod
    def _collect_perplexity_sources(result_str: str, sources: list[dict[str, str]]) -> None:
        """Extract citation URLs from a perplexity_research result and append as source objects."""
        try:
            data = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            return
        citations = data.get("citations")
        if not isinstance(citations, list):
            return
        seen_urls = {s["url"] for s in sources}
        for url in citations:
            url = str(url or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            try:
                parsed = urlparse(url)
                domain = parsed.netloc.replace("www.", "")
            except Exception:
                domain = url
            sources.append({"url": url, "domain": domain, "title": domain or url})

    @staticmethod
    def _collect_native_search_sources(raw_block: dict[str, Any], sources: list[dict[str, str]]) -> None:
        """Extract source URLs from an Anthropic web_search_tool_result block."""
        content = raw_block.get("content")
        if not isinstance(content, list):
            return
        seen_urls = {s["url"] for s in sources}
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "web_search_result":
                continue
            url = str(item.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            title = str(item.get("title") or "").strip()
            try:
                parsed = urlparse(url)
                domain = parsed.netloc.replace("www.", "")
            except Exception:
                domain = url
            sources.append({"url": url, "title": title or domain, "domain": domain})

    def _build_server_tool_loop_message(self, blocks: list[ContentBlock]) -> str:
        search_labels: list[str] = []
        fetch_targets: list[str] = []
        search_queries: list[str] = []
        saw_code_execution = False
        server_tool_count = 0

        for block in blocks:
            if block.block_type == "server_tool_use":
                server_tool_count += 1
                tool_input = self._parse_tool_input_json(block.input_json)
                if block.tool_name == "web_search":
                    query = self._activity_excerpt(tool_input.get("query"), limit=80)
                    if query:
                        search_queries.append(query)
                elif block.tool_name == "web_fetch":
                    target = self._activity_url_label(tool_input.get("url"))
                    if target:
                        fetch_targets.append(target)
                elif block.tool_name == "code_execution":
                    saw_code_execution = True
                continue

            if block.block_type == "web_search_tool_result" and block.raw_block:
                search_labels.extend(self._extract_native_search_labels(block.raw_block, limit=2))
                continue

            if block.block_type == "web_fetch_tool_result" and block.raw_block:
                target = self._extract_native_fetch_label(block.raw_block)
                if target:
                    fetch_targets.append(target)
                continue

            if block.block_type == "code_execution_tool_result":
                saw_code_execution = True

        phrases: list[str] = []
        if search_labels:
            phrases.append(self._format_found_pages_phrase("web search found", search_labels))
        elif search_queries:
            if len(search_queries) == 1:
                phrases.append(f'searched the web for "{search_queries[0]}"')
            else:
                phrases.append(f"ran {len(search_queries)} web searches")

        unique_fetch_targets = self._dedupe_preserve_order(fetch_targets)
        if unique_fetch_targets:
            phrases.append(self._format_found_pages_phrase("fetched page", unique_fetch_targets))

        if saw_code_execution:
            phrases.append("ran server-side code execution")

        if not phrases:
            return "Server-side tools continuing..."
        return self._compose_tool_loop_message(phrases, parallel=server_tool_count > 1)

    def _build_local_tool_loop_message(
        self,
        tool_blocks: list[ContentBlock],
        parsed_inputs: list[dict[str, Any]],
        result_strs: list[str],
        *,
        parallel: bool,
    ) -> str:
        phrases: list[str] = []
        for block, tool_input, result_str in zip(tool_blocks, parsed_inputs, result_strs):
            phrase = self._summarize_local_tool_activity(block.tool_name, tool_input, result_str)
            if phrase:
                phrases.append(phrase)

        if not phrases:
            tool_names = [block.tool_name for block in tool_blocks if block.tool_name]
            if not tool_names:
                return "Tool work completed. Continuing..."
            phrases.append(self._format_found_pages_phrase("completed tool work for", tool_names))
        return self._compose_tool_loop_message(phrases, parallel=parallel)

    def _summarize_local_tool_activity(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        result_str: str,
    ) -> str | None:
        data = self._parse_tool_result_json(result_str)

        if tool_name == "memory_search":
            query = self._activity_excerpt(tool_input.get("query"), limit=72)
            result_items = self._extract_result_items(data)
            if query and result_items:
                return f'searched memory for "{query}" and found {len(result_items)} hits'
            if query:
                return f'searched memory for "{query}"'
            if result_items:
                return f"searched memory and found {len(result_items)} hits"
            return "searched memory"

        if tool_name == "memory_fetch":
            if isinstance(data, dict) and data.get("found") is False:
                memory_id = self._activity_excerpt(tool_input.get("memory_id"), limit=48)
                if memory_id:
                    return f"checked full memory block {memory_id}"
                return "checked a full memory block"
            title = self._activity_excerpt(
                (data or {}).get("title") or ((data or {}).get("record") or {}).get("title"),
                limit=72,
            )
            if title:
                return f'loaded full memory block "{title}"'
            memory_id = self._activity_excerpt((data or {}).get("memory_id") or tool_input.get("memory_id"), limit=48)
            if memory_id:
                return f"loaded full memory block {memory_id}"
            return "loaded a full memory block"

        if tool_name == "session_revisit":
            session_id = self._activity_excerpt(
                ((data or {}).get("session") or {}).get("session_id") or tool_input.get("session_id"),
                limit=48,
            )
            if session_id:
                return f"revisited exact history for {session_id}"
            return "revisited exact session history"

        if tool_name == "session_history":
            session_id = self._activity_excerpt((data or {}).get("session_id") or tool_input.get("session_id"), limit=48)
            message_count = None
            if isinstance(data, dict):
                raw_messages = data.get("messages")
                if isinstance(raw_messages, list):
                    message_count = len(raw_messages)
            if session_id and message_count is not None:
                return f"loaded {message_count} messages from {session_id}"
            if session_id:
                return f"loaded detailed history for {session_id}"
            return "loaded detailed session history"

        if tool_name == "session_turns":
            session_id = self._activity_excerpt((data or {}).get("session_id") or tool_input.get("session_id"), limit=48)
            turn_count = None
            if isinstance(data, dict):
                turns = data.get("turns")
                if isinstance(turns, list):
                    turn_count = len(turns)
            if session_id and turn_count is not None:
                return f"reviewed {turn_count} turn summaries from {session_id}"
            if session_id:
                return f"reviewed turn summaries from {session_id}"
            return "reviewed session turn summaries"

        if tool_name == "session_state":
            session_id = self._activity_excerpt((data or {}).get("session_id") or tool_input.get("session_id"), limit=48)
            if session_id:
                return f"loaded session state for {session_id}"
            return "loaded session state"

        if tool_name == "task_notebook":
            task_id = self._activity_excerpt((data or {}).get("task_id") or tool_input.get("task_id"), limit=48)
            if isinstance(data, dict) and data.get("found") is False:
                if task_id:
                    return f"checked task notebook for {task_id}"
                return "checked the task notebook"
            if task_id:
                return f"loaded task notebook for {task_id}"
            return "loaded the task notebook"

        if tool_name == "perplexity_research":
            query = self._activity_excerpt(tool_input.get("query"), limit=72)
            if query:
                return f'completed deep research for "{query}"'
            return "completed deep research"

        if tool_name == "memory_write":
            return self._activity_phrase_from_result_message(data) or "saved durable memory"

        if tool_name == "memory_write_core_fact":
            return self._activity_phrase_from_result_message(data) or "saved a core fact"

        if tool_name == "create_reminder":
            return self._activity_phrase_from_result_message(data) or "created a reminder"

        if tool_name == "delete_reminder":
            return self._activity_phrase_from_result_message(data) or "deleted a reminder"

        if tool_name == "list_reminders":
            reminders = (data or {}).get("reminders") if isinstance(data, dict) else None
            if isinstance(reminders, list):
                return f"checked {len(reminders)} reminders"
            return "checked reminders"

        return None

    @staticmethod
    def _parse_tool_input_json(input_json: str) -> dict[str, Any]:
        try:
            payload = json.loads(input_json) if input_json else {}
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _parse_tool_result_json(result_str: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _dedupe_preserve_order(values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            normalized = str(value or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    def _extract_native_search_labels(self, raw_block: dict[str, Any], *, limit: int) -> list[str]:
        content = raw_block.get("content")
        if not isinstance(content, list):
            return []
        labels: list[str] = []
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "web_search_result":
                continue
            title = self._activity_excerpt(item.get("title"), limit=72)
            domain = self._activity_url_domain(item.get("url"))
            label = title or domain
            if title and domain:
                label = f"{title} ({domain})"
            if not label:
                continue
            labels.append(label)
            if len(labels) >= limit:
                break
        return self._dedupe_preserve_order(labels)

    def _extract_native_fetch_label(self, raw_block: dict[str, Any]) -> str | None:
        title = self._activity_excerpt(raw_block.get("title"), limit=72)
        domain = self._activity_url_domain(raw_block.get("url"))
        if title and domain:
            return f"{title} ({domain})"
        if title:
            return title
        if domain:
            return domain
        return None

    @staticmethod
    def _extract_result_items(data: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not isinstance(data, dict):
            return []
        for key in ("items", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    def _activity_phrase_from_result_message(self, data: dict[str, Any] | None) -> str | None:
        if not isinstance(data, dict):
            return None
        message = self._activity_excerpt(data.get("message"), limit=96)
        if not message:
            return None
        normalized = message.rstrip(".")
        if not normalized:
            return None
        return normalized[0].lower() + normalized[1:] if len(normalized) > 1 else normalized.lower()

    @staticmethod
    def _compose_tool_loop_message(phrases: list[str], *, parallel: bool) -> str:
        cleaned = [phrase.strip().rstrip(".") for phrase in phrases if str(phrase or "").strip()]
        if not cleaned:
            return "Tool work completed. Continuing..."
        if len(cleaned) == 1:
            sentence = cleaned[0]
            return sentence[0].upper() + sentence[1:] + ". Continuing..."

        prefix = "Completed parallel tool work: " if parallel else "Completed tool work: "
        preview = "; ".join(cleaned[:2])
        if len(cleaned) > 2:
            preview += f"; plus {len(cleaned) - 2} more"
        return prefix + preview + ". Continuing..."

    @staticmethod
    def _format_found_pages_phrase(prefix: str, labels: list[str]) -> str:
        cleaned = [label.strip() for label in labels if str(label or "").strip()]
        if not cleaned:
            return prefix
        preview = ", ".join(cleaned[:2])
        if len(cleaned) > 2:
            preview += f", plus {len(cleaned) - 2} more"
        return f"{prefix}: {preview}"

    def _activity_excerpt(self, value: Any, *, limit: int) -> str | None:
        normalized = " ".join(str(value or "").split())
        if not normalized:
            return None
        if len(normalized) <= limit:
            return normalized
        return normalized[: max(0, limit - 3)].rstrip() + "..."

    def _activity_url_domain(self, value: Any) -> str | None:
        url = str(value or "").strip()
        if not url:
            return None
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.replace("www.", "").strip()
        except Exception:
            domain = ""
        return self._activity_excerpt(domain or url, limit=60)

    def _activity_url_label(self, value: Any) -> str | None:
        url = str(value or "").strip()
        if not url:
            return None
        try:
            parsed = urlparse(url)
            if parsed.netloc:
                path = parsed.path.rstrip("/")
                if path and path != "/":
                    return self._activity_excerpt(f"{parsed.netloc}{path}", limit=84)
                return self._activity_excerpt(parsed.netloc, limit=84)
        except Exception:
            pass
        return self._activity_excerpt(url, limit=84)

    # ════════════════════════════════════════════════════════════
    #  User input relay
    # ════════════════════════════════════════════════════════════

    async def _user_reply_consumer_loop(self) -> None:
        assert self._redis is not None
        consumer_name = f"orchestrator-{id(self)}"
        while True:
            entries = await self._redis.xreadgroup(
                groupname=self.config.task_input_orchestrator_group,
                consumername=consumer_name,
                streams={self.config.task_input_replies_stream: ">"},
                count=5,
                block=1000,
            )
            for _stream, msgs in entries:
                for message_id, data in msgs:
                    try:
                        reply = parse_stream_payload(data)
                        irid = str(reply.get("input_request_id") or "").strip()
                        if not irid:
                            raise ValueError("input_request_id is required.")
                        content = str(reply.get("content") or "").strip()
                        self.task_ledger.mark_task_input_replied(irid, content=content)
                        future = self._pending_input_futures.get(irid)
                        if future is not None and not future.done():
                            future.set_result(reply)
                        self._pending_input_futures.pop(irid, None)
                        await self._redis.xack(
                            self.config.task_input_replies_stream,
                            self.config.task_input_orchestrator_group,
                            message_id,
                        )
                    except Exception:
                        continue
