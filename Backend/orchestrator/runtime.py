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
import re
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
    MeteredCall,
    SOURCE_PRIORITY_MAP,
    TaskEnvelope,
    TaskInProgress,
    begin_metered_call,
    build_model_key,
    build_usage_event,
    create_redis_client,
    dispatch_task,
    ensure_stream_group,
    generate_task_id,
    parse_event_envelope,
    parse_stream_payload,
    post_usage_event,
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
    # Raw block for opaque server-side result blocks (*_tool_result)
    raw_block: dict[str, Any] | None = None

    @staticmethod
    def _is_server_tool_result_block(block_type: str) -> bool:
        return block_type.endswith("_tool_result")

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
        if self._is_server_tool_result_block(self.block_type):
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
        self._featured_specialists_task: asyncio.Task[None] | None = None
        self._tool_executor: ToolExecutor | None = None
        self._anthropic_loop_stats = AnthropicLoopStats()
        self._agent_dispatch_stats = AgentDispatchStats()
        self._agent_event_consumer_name = f"orchestrator-events-{id(self)}"
        self._featured_specialists_cache: list[dict[str, Any]] = []
        self._featured_specialists_refreshed_at: float = 0.0

    def _featured_specialist_agent_ids(self) -> set[str]:
        return {
            str(item.get("agent_id") or "").strip()
            for item in self._featured_specialists_cache
            if isinstance(item, dict) and str(item.get("agent_id") or "").strip()
        }

    async def start(self) -> None:
        self.task_ledger.initialize()
        self.registry_store.initialize()
        self._refresh_featured_specialists(force=True)
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
        if self.config.featured_specialists_enabled and self.config.featured_specialists_refresh_sec > 0:
            self._featured_specialists_task = asyncio.create_task(
                self._featured_specialists_refresh_loop(),
                name="orchestrator-featured-specialists-refresh",
            )

        self._tool_executor = ToolExecutor(
            perplexity_api_key=self.config.perplexity_api_key,
            perplexity_model=self.config.perplexity_model,
            cosmic_memory_url=self.config.cosmic_memory_url,
            gateway_url=self.config.gateway_url,
            gateway_internal_token=self.config.internal_token,
            agent_dispatcher=self.dispatch_agent_task,
            agent_catalog_searcher=self.search_agent_catalog,
        )
        self.started = True

    async def stop(self) -> None:
        if self._featured_specialists_task is not None:
            self._featured_specialists_task.cancel()
            await asyncio.gather(self._featured_specialists_task, return_exceptions=True)
            self._featured_specialists_task = None
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
            parent_task=task,
        )

        try:
            messages = self._build_messages(task)
            self._refresh_featured_specialists()
            system_prompt = build_agentic_system_prompt(
                str(task.input.get("memory_context") or "").strip() or None,
                user_timezone=str(task.input.get("user_timezone") or "").strip() or None,
                featured_specialists=self._featured_specialists_cache,
            )
            tools = get_model_tool_definitions(self._featured_specialist_agent_ids())
            max_iterations = self.config.max_tool_iterations

            iteration = 0
            full_response_text = ""
            full_reasoning_text = ""
            collected_sources: list[dict[str, str]] = []
            produced_artifacts: list[dict[str, Any]] = []
            research_paths: set[str] = set()
            specialist_receipts: list[dict[str, Any]] = []
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
                    usage_context={
                        "task_id": task.task_id,
                        "request_id": request_id,
                        "session_id": session_id,
                        "route": "opus",
                        "operation": "orchestrator.process",
                        "metadata_json": {
                            "iteration": iteration,
                            "source": task.source,
                            "source_id": task.source_id,
                            "channel": channel,
                        },
                    },
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
                        elif ContentBlock._is_server_tool_result_block(btype):
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
                    elif b.block_type == "server_tool_use" or ContentBlock._is_server_tool_result_block(b.block_type):
                        turn_server_blocks.append(b)
                        if b.tool_name == "web_search" or b.block_type == "web_search_tool_result":
                            research_paths.add("native_web_search")
                        elif b.tool_name == "web_fetch" or b.block_type == "web_fetch_tool_result":
                            research_paths.add("native_web_fetch")
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
                            research_paths.add("perplexity_research")
                            self._collect_perplexity_sources(result_str, collected_sources)
                        elif tb.tool_name in {"firecrawl_scrape", "firecrawl_extract", "firecrawl_recall_session"}:
                            research_paths.add("firecrawl")
                        elif tb.tool_name in {"x_search", "x_recall_session"}:
                            research_paths.add("x_search_specialist")
                            self._collect_x_specialist_sources(result_str, collected_sources)
                        elif tb.tool_name == "delegate_to_agent":
                            self._inherit_specialist_research_provenance(
                                result_str,
                                research_paths=research_paths,
                                sources=collected_sources,
                            )
                        self._collect_specialist_artifacts(
                            result_str,
                            produced_artifacts=produced_artifacts,
                        )
                        self._collect_specialist_receipt(
                            tb.tool_name,
                            pi,
                            result_str,
                            specialist_receipts=specialist_receipts,
                        )

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
            research_provenance = self._build_research_provenance(
                research_paths=research_paths,
                sources=collected_sources,
            )
            if research_provenance:
                complete_event["research_provenance"] = research_provenance
            if collected_sources:
                complete_event["sources"] = collected_sources
            if specialist_receipts:
                complete_event["specialist_receipts"] = specialist_receipts
            if produced_artifacts:
                complete_event["produced_artifacts"] = produced_artifacts
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
            card = self.registry_store.get_card(agent_id) if agent_id else None
            description = ""
            if isinstance(card, dict):
                raw_desc = card.get("description")
                if isinstance(raw_desc, str):
                    description = raw_desc.strip()
                elif raw_desc is not None:
                    description = str(raw_desc).strip()
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
                    "description": description,
                    "intents": intents,
                    "healthy_instance": healthy_instance,
                    "instance_id": instance_id,
                }
            )
        return results

    async def search_agent_catalog(
        self,
        *,
        query: str,
        limit: int = 5,
        require_healthy: bool = True,
    ) -> dict[str, Any]:
        normalized_query = str(query or "").strip()
        query_tokens = self._catalog_tokens(normalized_query)
        cards = self.registry_store.list_agent_cards(status="registered")
        matches: list[dict[str, Any]] = []

        for card in cards:
            if not isinstance(card, dict):
                continue
            agent_id = str(card.get("agent_id") or "").strip()
            if not agent_id:
                continue

            healthy = None
            instance_id: str | None = None
            if self._redis is not None:
                found_agent_id, found_instance_id = await find_available_instance_for_agent(agent_id, self._redis)
                healthy = bool(found_agent_id and found_instance_id)
                instance_id = found_instance_id
                if require_healthy and not healthy:
                    continue

            agent_display_name = str(card.get("display_name") or agent_id).strip() or agent_id
            agent_description = str(card.get("description") or "").strip()
            for raw_intent in card.get("intents", []):
                if not isinstance(raw_intent, dict):
                    continue
                intent_name = str(raw_intent.get("name") or "").strip()
                if not intent_name:
                    continue
                score = self._score_catalog_match(
                    normalized_query=normalized_query,
                    query_tokens=query_tokens,
                    agent_display_name=agent_display_name,
                    agent_id=agent_id,
                    agent_description=agent_description,
                    intent_name=intent_name,
                    intent_description=str(raw_intent.get("description") or "").strip(),
                )
                if normalized_query and score <= 0:
                    continue
                matches.append(
                    {
                        "intent": intent_name,
                        "intent_description": str(raw_intent.get("description") or "").strip(),
                        "agent_id": agent_id,
                        "display_name": agent_display_name,
                        "agent_description": agent_description,
                        "healthy": healthy,
                        "instance_id": instance_id,
                        "timeout_sec": raw_intent.get("timeout_sec"),
                        "input_schema_summary": raw_intent.get("input_schema_summary") or {},
                        "output_schema_summary": raw_intent.get("output_schema_summary") or {},
                        "usage_hints": [
                            str(item).strip()
                            for item in (raw_intent.get("usage_hints") or [])
                            if str(item).strip()
                        ],
                        "_score": score,
                    }
                )

        matches.sort(
            key=lambda item: (
                int(item.get("_score") or 0),
                1 if item.get("healthy") else 0,
                str(item.get("intent") or ""),
            ),
            reverse=True,
        )
        limited_matches = matches[: max(1, min(limit, 20))]
        for item in limited_matches:
            item.pop("_score", None)

        message = (
            f"Found {len(limited_matches)} matching specialist intents."
            if limited_matches else
            "No matching specialist intents found."
        )
        return {
            "query": normalized_query,
            "require_healthy": require_healthy,
            "matches": limited_matches,
            "count": len(limited_matches),
            "message": message,
        }

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
            "featured_specialists": self._featured_specialists_cache,
            "agents": agents,
        }

    @staticmethod
    def _catalog_tokens(query: str) -> list[str]:
        if not query:
            return []
        return [token for token in re.split(r"[^a-z0-9]+", query.lower()) if token]

    def _score_catalog_match(
        self,
        *,
        normalized_query: str,
        query_tokens: list[str],
        agent_display_name: str,
        agent_id: str,
        agent_description: str,
        intent_name: str,
        intent_description: str,
    ) -> int:
        if not normalized_query:
            return 1

        agent_text = " ".join((agent_display_name, agent_id, agent_description)).lower()
        intent_text = " ".join((intent_name, intent_description)).lower()
        score = 0

        if normalized_query.lower() in intent_text:
            score += 12
        if normalized_query.lower() in agent_text:
            score += 8

        for token in query_tokens:
            if token in intent_text:
                score += 4
            if token in agent_text:
                score += 2
        return score

    async def dispatch_agent_task(
        self,
        *,
        parent_task: TaskEnvelope,
        intent: str,
        input_payload: dict[str, Any] | None = None,
        input_artifacts: list[dict[str, Any]] | None = None,
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
            input_artifacts=[item for item in (input_artifacts or []) if isinstance(item, dict)],
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

    async def _featured_specialists_refresh_loop(self) -> None:
        interval = max(30, int(self.config.featured_specialists_refresh_sec))
        while True:
            try:
                self._refresh_featured_specialists(force=True)
            except Exception:
                logger.exception("orchestrator.featured_specialists_refresh_failed")
            await asyncio.sleep(interval)

    def _refresh_featured_specialists(self, *, force: bool = False) -> None:
        if not self.config.featured_specialists_enabled:
            self._featured_specialists_cache = []
            self._featured_specialists_refreshed_at = time.monotonic()
            return
        now_monotonic = time.monotonic()
        refresh_interval = max(30, int(self.config.featured_specialists_refresh_sec))
        if not force and self._featured_specialists_refreshed_at and (now_monotonic - self._featured_specialists_refreshed_at) < refresh_interval:
            return
        self._featured_specialists_cache = self.registry_store.refresh_featured_specialists(
            limit=self.config.featured_specialists_count,
            lookback_days=self.config.featured_specialists_lookback_days,
        )
        self._featured_specialists_refreshed_at = now_monotonic

    def _refresh_featured_specialists_after_usage(self) -> None:
        if not self.config.featured_specialists_enabled:
            return
        self._refresh_featured_specialists(force=True)

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
        future = self._pending_agent_results.get(task_id)
        if future is None:
            return
        aliases = [key for key, value in self._pending_agent_results.items() if value is future]
        for key in aliases:
            self._pending_agent_results.pop(key, None)
        if not future.done():
            future.set_result(result)

    def _link_pending_agent_result_alias(self, *, alias_task_id: str, canonical_task_id: str) -> None:
        future = self._pending_agent_results.get(canonical_task_id)
        if future is not None and not future.done():
            self._pending_agent_results[alias_task_id] = future

    def _register_task_input_wait_from_suspension(self, task_id: str, payload: dict[str, Any]) -> None:
        input_request_id = str(payload.get("input_request_id") or "").strip()
        if not input_request_id:
            return
        task_record = self.task_ledger.get_task(task_id)
        if task_record is None:
            return
        resume_payload = payload.get("resume_payload") if isinstance(payload.get("resume_payload"), dict) else {}
        resume_intent = str(payload.get("resume_intent") or "agent.resume").strip() or "agent.resume"
        self.task_ledger.create_task_input_wait(
            input_request_id=input_request_id,
            waiting_task_id=task_id,
            parent_task_id=str(task_record.get("parent_task_id") or "").strip() or None,
            recipient=str(task_record.get("recipient") or "").strip(),
            resume_intent=resume_intent,
            resume_payload=resume_payload,
        )

    async def _dispatch_resumed_task_for_input_reply(self, reply: dict[str, Any]) -> None:
        if self._redis is None:
            return
        input_request_id = str(reply.get("input_request_id") or "").strip()
        if not input_request_id:
            return
        wait = self.task_ledger.get_task_input_wait(input_request_id)
        if wait is None or str(wait.get("status") or "").strip() != "pending":
            return

        waiting_task_id = str(wait.get("waiting_task_id") or "").strip()
        waiting_record = self.task_ledger.get_task(waiting_task_id)
        if waiting_record is None:
            return
        waiting_envelope = waiting_record.get("envelope_json") if isinstance(waiting_record.get("envelope_json"), dict) else {}
        resume_input = {
            "resume_of_task_id": waiting_task_id,
            "resume_intent": str(waiting_record.get("intent") or "").strip(),
            "resume_input": dict(waiting_envelope.get("input") or {}),
            "resume_state": wait.get("resume_payload_json") if isinstance(wait.get("resume_payload_json"), dict) else {},
            "reply": dict(reply),
            "input_request_id": input_request_id,
        }
        resume_task = TaskEnvelope(
            task_id=generate_task_id(),
            task_list_id=str(waiting_record.get("task_list_id") or "").strip(),
            parent_task_id=waiting_task_id,
            session_id=str(waiting_record.get("session_id") or "").strip() or None,
            sender=self.config.orchestrator_agent_id,
            recipient=str(wait.get("recipient") or waiting_record.get("recipient") or "").strip(),
            intent=str(wait.get("resume_intent") or "agent.resume").strip() or "agent.resume",
            input=resume_input,
            input_artifacts=list(waiting_envelope.get("input_artifacts") or []),
            idempotency_key=f"{str(waiting_record.get('idempotency_key') or '').strip()}:resume:{input_request_id}",
            deadline_ts=None,
            priority=str(waiting_record.get("priority") or "normal").strip() or "normal",
            leader_epoch=None,
            signature="",
            source=str(waiting_record.get("source") or "agent").strip() or "agent",
            source_id=str(waiting_record.get("source_id") or "").strip() or None,
            channel=str(waiting_record.get("channel") or "").strip() or None,
        )
        signature = sign_task_envelope(resume_task, self._resolve_agent_secret(resume_task.recipient))
        resume_task = resume_task.model_copy(update={"signature": signature})
        self.task_ledger.create_task(resume_task)
        self.task_ledger.mark_task_input_wait_resumed(input_request_id, resumed_task_id=resume_task.task_id)
        self.task_ledger.mark_resumed(
            waiting_task_id,
            payload={
                "input_request_id": input_request_id,
                "resume_task_id": resume_task.task_id,
                "reply_excerpt": str(reply.get("content") or "").strip()[:2000],
            },
        )
        self._link_pending_agent_result_alias(alias_task_id=resume_task.task_id, canonical_task_id=waiting_task_id)
        await dispatch_task(resume_task, self._redis)

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
        resume_wait = self.task_ledger.get_task_input_wait_by_resumed_task(event.task_id)
        canonical_task_id = str(resume_wait.get("waiting_task_id") or "").strip() if resume_wait else event.task_id

        if event.event_type == "task.completed":
            task_record = self.task_ledger.get_task(canonical_task_id)
            result = self._coerce_agent_result(event)
            self.task_ledger.mark_completed(event.task_id, result=result.model_dump(mode="json"))
            if canonical_task_id != event.task_id:
                self.task_ledger.mark_completed(canonical_task_id, result=result.model_dump(mode="json"))
            self._agent_dispatch_stats.dispatches_completed += 1
            self._record_successful_specialist_usage(task_record)
            self._resolve_pending_agent_result(canonical_task_id, result)
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
            if canonical_task_id != event.task_id:
                self.task_ledger.mark_failed(canonical_task_id, code=error.code, message=error.message)
            self._agent_dispatch_stats.failed_events += 1
            self._resolve_pending_agent_result(canonical_task_id, result)
            return

        if event.event_type == "task.deferred":
            task_record = self.task_ledger.get_task(canonical_task_id)
            if task_record is not None and str(task_record.get("status") or "").strip() == "suspended":
                self._agent_dispatch_stats.deferred_events += 1
                return
            try:
                result = TaskInProgress.model_validate(event.payload)
            except Exception as exc:
                result = self._build_in_progress_result(
                    canonical_task_id,
                    idempotency_key=f"deferred:{canonical_task_id}",
                    timeout_sec=30,
                )
                logger.warning("orchestrator.agent_event_invalid_deferred task_id=%s error=%s", event.task_id, exc)
            self.task_ledger.mark_deferred(event.task_id, result=result.model_dump(mode="json"))
            if canonical_task_id != event.task_id:
                self.task_ledger.mark_deferred(canonical_task_id, result=result.model_dump(mode="json"))
            self._agent_dispatch_stats.deferred_events += 1
            self._resolve_pending_agent_result(canonical_task_id, result)
            return

        if event.event_type == "task.suspended":
            self.task_ledger.mark_suspended(canonical_task_id, payload=event.payload)
            self._register_task_input_wait_from_suspension(canonical_task_id, event.payload)
            return

        if event.event_type == "task.resumed":
            self.task_ledger.mark_resumed(canonical_task_id, payload=event.payload)
            return

        if event.event_type == "task.rejected":
            # Current workers use task.rejected for hard dispatch rejection.
            # When epoch-based redrive lands, this branch can become redispatch-aware.
            result = self._rejected_agent_result(event)
            error = result.error
            assert error is not None
            self.task_ledger.mark_failed(event.task_id, code=error.code, message=error.message)
            if canonical_task_id != event.task_id:
                self.task_ledger.mark_failed(canonical_task_id, code=error.code, message=error.message)
            self._agent_dispatch_stats.rejected_events += 1
            self._resolve_pending_agent_result(canonical_task_id, result)
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
        usage_context: dict[str, Any] | None = None,
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
            usage: dict[str, Any] = {}
            metered_call = begin_metered_call(prefix="call")
            provider_request_id: str | None = None
            try:
                async with self._client.stream("POST", url, headers=headers, json=body) as resp:
                    provider_request_id = (
                        resp.headers.get("request-id")
                        or resp.headers.get("x-request-id")
                        or resp.headers.get("anthropic-request-id")
                        or None
                    )
                    if resp.status_code >= 400:
                        raw = await resp.aread()
                        raise RuntimeError(self._error_from_response(raw, resp.status_code))
                    async for item in self._iter_sse(resp):
                        yielded_any = True
                        if item.data:
                            try:
                                payload = json.loads(item.data)
                            except Exception:
                                payload = None
                            if isinstance(payload, dict):
                                ptype = str(payload.get("type") or "")
                                if ptype == "message_start":
                                    message = payload.get("message")
                                    if isinstance(message, dict):
                                        usage = self._merge_usage(usage, message.get("usage"))
                                elif ptype == "message_delta":
                                    usage = self._merge_usage(usage, payload.get("usage"))
                        yield item
                await self._record_internal_usage_event(
                    metered_call=metered_call,
                    model_key=build_model_key("anthropic", self.config.anthropic_model),
                    usage_context=usage_context,
                    provider_request_id=provider_request_id,
                    raw_usage=usage,
                    success=True,
                    error_code=None,
                    metadata_json={
                        "attempt": attempt + 1,
                        "streaming": True,
                        "yielded_any": yielded_any,
                        "container_id": container_id,
                    },
                )
                return
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                await self._record_internal_usage_event(
                    metered_call=metered_call,
                    model_key=build_model_key("anthropic", self.config.anthropic_model),
                    usage_context=usage_context,
                    provider_request_id=provider_request_id,
                    raw_usage=usage,
                    success=False,
                    error_code=type(exc).__name__,
                    metadata_json={
                        "attempt": attempt + 1,
                        "streaming": True,
                        "yielded_any": yielded_any,
                        "container_id": container_id,
                    },
                )
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

    async def _record_internal_usage_event(
        self,
        *,
        metered_call: MeteredCall,
        model_key: str,
        usage_context: dict[str, Any] | None,
        provider_request_id: str | None,
        raw_usage: Any,
        success: bool,
        error_code: str | None,
        metadata_json: dict[str, Any] | None,
    ) -> None:
        base_context = usage_context if isinstance(usage_context, dict) else {}
        event = build_usage_event(
            metered_call=metered_call,
            source_component="orchestrator",
            source_id=self.config.orchestrator_agent_id,
            task_id=str(base_context.get("task_id") or "").strip() or None,
            session_id=str(base_context.get("session_id") or "").strip() or None,
            route=str(base_context.get("route") or "").strip() or None,
            operation=str(base_context.get("operation") or "orchestrator.process").strip(),
            model_key=model_key,
            request_id=str(base_context.get("request_id") or "").strip() or None,
            provider_request_id=provider_request_id,
            raw_usage=raw_usage,
            success=success,
            error_code=error_code,
            metadata_json=self._merge_usage_metadata(
                base_context.get("metadata_json"),
                metadata_json,
            ),
        )
        try:
            posted = await post_usage_event(
                client=self._client,
                gateway_url=self.config.gateway_url,
                internal_token=self.config.internal_token,
                event=event,
            )
            if not posted:
                logger.warning(
                    "orchestrator.usage_post_failed llm_call_id=%s operation=%s model=%s",
                    event.llm_call_id,
                    event.operation,
                    event.model,
                )
        except Exception:
            logger.exception(
                "orchestrator.usage_post_exception llm_call_id=%s operation=%s model=%s",
                event.llm_call_id,
                event.operation,
                event.model,
            )

    def _merge_usage_metadata(self, primary: Any, secondary: Any) -> Any:
        if primary is None and secondary is None:
            return None
        if primary is None:
            return secondary
        if secondary is None:
            return primary
        if isinstance(primary, dict) and isinstance(secondary, dict):
            return {
                **primary,
                **secondary,
            }
        if isinstance(primary, dict):
            return {
                **primary,
                "extra": secondary,
            }
        if isinstance(secondary, dict):
            return {
                **secondary,
                "extra": primary,
            }
        return {
            "primary": primary,
            "secondary": secondary,
        }

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
            content = self._normalize_message_content(item.get("content"))
            if role not in {"user", "assistant"} or self._message_content_is_empty(content):
                continue
            # Collapse consecutive same-role messages (safety)
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] = self._merge_message_content(messages[-1]["content"], content)
                continue
            messages.append({"role": role, "content": content})

        # Strip leading assistant messages (API requires first message is user)
        while messages and messages[0]["role"] != "user":
            messages.pop(0)

        # ── Build the current user query ──────────────────────────
        user_query = str(task.input.get("query") or "").strip()
        input_artifacts = task.input_artifacts if isinstance(task.input_artifacts, list) else []
        image_blocks: list[dict[str, Any]] = []
        max_input_images = max(1, int(self.config.anthropic_max_input_images))
        omitted_image_blocks = 0
        if input_artifacts:
            manifest_lines = [
                "The user attached media/artifacts with metadata below.",
                (
                    "You have metadata for every attachment. For image attachments, only the image blocks in this same message count as direct visual input. "
                    "Do not claim to have directly viewed or listened to bytes unless an inline image block or a tool actually loaded them."
                ),
                "", "Attachment manifest:",
            ]
            for i, art in enumerate(input_artifacts, 1):
                if not isinstance(art, dict):
                    continue
                parts = [
                    f"kind={str(art.get('kind') or 'unknown').strip()}",
                    f"mime={str(art.get('mime') or 'application/octet-stream').strip()}",
                ]
                for key in ("filename", "caption", "bridge_media_ref", "download_url", "ingest_state", "parse_bundle_id"):
                    v = str(art.get(key) or "").strip()
                    if v:
                        parts.append(f"{key}={v}")
                provider_url = str(art.get("provider_url") or "").strip()
                if provider_url and str(art.get("kind") or "").strip().lower() == "image":
                    if len(image_blocks) < max_input_images:
                        parts.append("model_input=image_block")
                        image_blocks.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "url",
                                    "url": provider_url,
                                },
                            }
                        )
                    else:
                        omitted_image_blocks += 1
                        parts.append("model_input=omitted_limit")
                parsed_summary = art.get("parsed_summary") if isinstance(art.get("parsed_summary"), dict) else None
                if parsed_summary is not None:
                    doc_title = str(parsed_summary.get("title") or "").strip()
                    doc_id = str(parsed_summary.get("doc_id") or "").strip()
                    if doc_title:
                        parts.append(f"parsed_title={doc_title}")
                    if doc_id:
                        parts.append(f"doc_id={doc_id}")
                    chunk_count = parsed_summary.get("chunk_count")
                    section_count = parsed_summary.get("section_count")
                    if chunk_count:
                        parts.append(f"chunk_count={chunk_count}")
                    if section_count:
                        parts.append(f"section_count={section_count}")
                sb = art.get("size_bytes")
                if sb:
                    parts.append(f"size_bytes={sb}")
                manifest_lines.append(f"{i}. " + "; ".join(parts))
            if omitted_image_blocks:
                manifest_lines.extend(
                    [
                        "",
                        (
                            f"{omitted_image_blocks} additional image attachment(s) were staged but not sent as inline visual input "
                            f"because the per-turn image cap is {max_input_images}."
                        ),
                    ]
                )
            user_query = user_query + "\n\n" + "\n".join(manifest_lines) if user_query else "\n".join(manifest_lines)

        current_user_content: str | list[dict[str, Any]] = user_query
        if image_blocks:
            text = user_query or "The user attached images. Analyze the inline image blocks together with the attachment metadata."
            current_user_content = [*image_blocks, {"type": "text", "text": text}]

        # Append the current query — if context somehow ends with user (e.g.
        # a response was never stored), collapse to avoid consecutive user turns.
        if messages and messages[-1]["role"] == "user":
            messages[-1]["content"] = self._merge_message_content(messages[-1]["content"], current_user_content)
        else:
            messages.append({"role": "user", "content": current_user_content})

        return messages

    def _normalize_message_content(self, value: Any) -> str | list[dict[str, Any]]:
        if isinstance(value, list):
            blocks = [item for item in value if isinstance(item, dict)]
            return blocks
        return str(value or "").strip()

    def _message_content_is_empty(self, value: Any) -> bool:
        if isinstance(value, list):
            return len([item for item in value if isinstance(item, dict)]) <= 0
        return not str(value or "").strip()

    def _content_to_blocks(self, value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        text = str(value or "").strip()
        if not text:
            return []
        return [{"type": "text", "text": text}]

    def _merge_message_content(self, existing: Any, incoming: Any) -> str | list[dict[str, Any]]:
        if isinstance(existing, list) or isinstance(incoming, list):
            return [*self._content_to_blocks(existing), *self._content_to_blocks(incoming)]
        existing_text = str(existing or "").strip()
        incoming_text = str(incoming or "").strip()
        if existing_text and incoming_text:
            return existing_text + "\n\n" + incoming_text
        return existing_text or incoming_text

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

    @staticmethod
    def _collect_x_specialist_sources(result_str: str, sources: list[dict[str, str]]) -> None:
        try:
            data = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(data, dict):
            return

        seen_urls = {s["url"] for s in sources}

        def append_source(url_value: Any, *, title_value: Any = None) -> None:
            url = str(url_value or "").strip()
            if not url or url in seen_urls:
                return
            seen_urls.add(url)
            title = str(title_value or "").strip()
            try:
                parsed = urlparse(url)
                domain = parsed.netloc.replace("www.", "")
            except Exception:
                domain = url
            sources.append({"url": url, "title": title or domain or url, "domain": domain})

        citations = data.get("citations")
        if isinstance(citations, list):
            for item in citations:
                if isinstance(item, str):
                    append_source(item)
                elif isinstance(item, dict):
                    append_source(item.get("url"), title_value=item.get("title"))

        posts = data.get("notable_posts")
        if isinstance(posts, list):
            for item in posts:
                if not isinstance(item, dict):
                    continue
                handle = str(item.get("author_handle") or "").strip().lstrip("@")
                title = f"@{handle} on X" if handle else "X Post"
                append_source(item.get("post_url"), title_value=title)

    def _inherit_specialist_research_provenance(
        self,
        result_str: str,
        *,
        research_paths: set[str],
        sources: list[dict[str, str]],
    ) -> None:
        try:
            data = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(data, dict):
            return

        delegation = data.get("delegation")
        intent_name = ""
        if isinstance(delegation, dict):
            intent_name = str(delegation.get("intent") or "").strip()

        if intent_name in {"x.search", "x.recall_session"}:
            research_paths.add("x_search_specialist")
            self._collect_x_specialist_sources(json.dumps(data), sources)
            return

        if intent_name.startswith("firecrawl."):
            research_paths.add("firecrawl")
            return

    def _collect_specialist_receipt(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        result_str: str,
        *,
        specialist_receipts: list[dict[str, Any]],
    ) -> None:
        data = self._parse_tool_result_json(result_str)
        if not isinstance(data, dict):
            return
        delegation = data.get("delegation")
        if not isinstance(delegation, dict):
            return

        intent_name = self._activity_excerpt(
            delegation.get("intent") or tool_input.get("intent"),
            limit=96,
        )
        agent_id = self._activity_excerpt(
            delegation.get("agent_id") or tool_input.get("agent_id"),
            limit=120,
        )
        if not intent_name and not agent_id:
            return

        activity = self._activity_excerpt(
            self._summarize_local_tool_activity(tool_name, tool_input, result_str),
            limit=160,
        )
        local_sources = self._extract_specialist_sources(data, intent_name=intent_name)
        artifact_count = len(data.get("artifacts")) if isinstance(data.get("artifacts"), list) else 0
        source_domains: list[str] = []
        if local_sources:
            seen_domains: set[str] = set()
            for item in local_sources:
                if not isinstance(item, dict):
                    continue
                domain = str(item.get("domain") or "").strip()
                if not domain or domain in seen_domains:
                    continue
                source_domains.append(domain)
                seen_domains.add(domain)
                if len(source_domains) >= 3:
                    break

        receipt: dict[str, Any] = {
            "tool_name": tool_name,
            "intent": intent_name,
            "agent_id": agent_id,
            "agent_label": self._activity_agent_label(agent_id),
            "activity": activity,
        }
        if intent_name == "tabular.create_workbook":
            bundle_id = self._activity_excerpt(data.get("bundle_id"), limit=96)
            workbooks = data.get("workbooks") if isinstance(data.get("workbooks"), list) else []
            first_workbook = workbooks[0] if workbooks and isinstance(workbooks[0], dict) else {}
            artifact_id = self._activity_excerpt(first_workbook.get("artifact_id"), limit=96)
            filename = self._activity_excerpt(first_workbook.get("filename"), limit=160)
            parse_status = self._activity_excerpt(first_workbook.get("parse_status"), limit=48)
            sheet_count = first_workbook.get("sheet_count")
            if bundle_id:
                receipt["bundle_id"] = bundle_id
            if artifact_id:
                receipt["artifact_id"] = artifact_id
            if filename:
                receipt["filename"] = filename
            if parse_status:
                receipt["parse_status"] = parse_status
            if isinstance(sheet_count, int):
                receipt["sheet_count"] = sheet_count
        if artifact_count > 0:
            receipt["artifact_count"] = artifact_count
        if local_sources:
            receipt["source_count"] = len(local_sources)
            receipt["source_domains"] = source_domains
            receipt["source_sample"] = local_sources[:2]

        dedupe_key = json.dumps(
            {
                "intent": receipt.get("intent"),
                "agent_id": receipt.get("agent_id"),
                "activity": receipt.get("activity"),
            },
            sort_keys=True,
        )
        existing_keys = {
            json.dumps(
                {
                    "intent": item.get("intent"),
                    "agent_id": item.get("agent_id"),
                    "activity": item.get("activity"),
                },
                sort_keys=True,
            )
            for item in specialist_receipts
            if isinstance(item, dict)
        }
        if dedupe_key in existing_keys:
            return
        specialist_receipts.append({key: value for key, value in receipt.items() if value not in (None, "", [], {})})
        if len(specialist_receipts) > 4:
            del specialist_receipts[:-4]

    def _collect_specialist_artifacts(
        self,
        result_str: str,
        *,
        produced_artifacts: list[dict[str, Any]],
    ) -> None:
        data = self._parse_tool_result_json(result_str)
        if not isinstance(data, dict):
            return
        raw_artifacts = data.get("artifacts")
        if not isinstance(raw_artifacts, list):
            return

        existing_keys = {
            (
                str(item.get("artifact_id") or "").strip(),
                str(item.get("path") or "").strip(),
            )
            for item in produced_artifacts
            if isinstance(item, dict)
        }
        for item in raw_artifacts:
            if not isinstance(item, dict):
                continue
            audience = self._activity_excerpt(item.get("audience"), limit=32) or "deliverable"
            if audience != "deliverable":
                continue
            artifact_id = self._activity_excerpt(item.get("artifact_id"), limit=160)
            path = self._activity_excerpt(item.get("path"), limit=400)
            mime = self._activity_excerpt(item.get("mime"), limit=160)
            task_id = self._activity_excerpt(item.get("task_id"), limit=160)
            kind = self._activity_excerpt(item.get("kind"), limit=64)
            created_by_agent = self._activity_excerpt(item.get("created_by_agent"), limit=160)
            source_url = self._activity_excerpt(item.get("source_url"), limit=400)
            sha256 = self._activity_excerpt(item.get("sha256"), limit=160)
            filename = self._activity_excerpt(item.get("filename"), limit=240)
            dedupe_key = (artifact_id or "", path or "")
            if not any(dedupe_key):
                continue
            if dedupe_key in existing_keys:
                continue
            existing_keys.add(dedupe_key)
            produced_artifacts.append(
                {
                    key: value
                    for key, value in {
                        "artifact_id": artifact_id,
                        "task_id": task_id,
                        "mime": mime,
                        "path": path,
                        "kind": kind,
                        "audience": audience,
                        "filename": filename,
                        "created_by_agent": created_by_agent,
                        "source_url": source_url,
                        "sha256": sha256,
                        "created_at": item.get("created_at"),
                    }.items()
                    if value not in (None, "", [], {})
                }
            )
            if len(produced_artifacts) >= 12:
                break

    def _extract_specialist_sources(
        self,
        data: dict[str, Any],
        *,
        intent_name: str | None = None,
    ) -> list[dict[str, str]]:
        sources: list[dict[str, str]] = []
        if intent_name in {"x.search", "x.recall_session"}:
            self._collect_x_specialist_sources(json.dumps(data), sources)

        raw_sources = data.get("sources")
        if isinstance(raw_sources, list):
            for item in raw_sources:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip()
                if not url or any(existing.get("url") == url for existing in sources):
                    continue
                title = str(item.get("title") or "").strip()
                domain = str(item.get("domain") or "").strip()
                if not domain:
                    try:
                        domain = urlparse(url).netloc.replace("www.", "")
                    except Exception:
                        domain = ""
                sources.append({"url": url, "title": title or domain or url, "domain": domain})

        citations = data.get("citations")
        if isinstance(citations, list):
            for item in citations:
                if isinstance(item, str):
                    url = item.strip()
                    title = ""
                elif isinstance(item, dict):
                    url = str(item.get("url") or "").strip()
                    title = str(item.get("title") or "").strip()
                else:
                    continue
                if not url or any(existing.get("url") == url for existing in sources):
                    continue
                try:
                    domain = urlparse(url).netloc.replace("www.", "")
                except Exception:
                    domain = ""
                sources.append({"url": url, "title": title or domain or url, "domain": domain})

        direct_url = self._activity_url_label(data.get("url"))
        if direct_url:
            url = str(data.get("url") or "").strip()
            if url and not any(existing.get("url") == url for existing in sources):
                try:
                    domain = urlparse(url).netloc.replace("www.", "")
                except Exception:
                    domain = ""
                sources.append({"url": url, "title": domain or url, "domain": domain})
        return sources

    @classmethod
    def _build_research_provenance(
        cls,
        *,
        research_paths: set[str],
        sources: list[dict[str, str]],
    ) -> dict[str, Any] | None:
        ordered_paths = [
            path
            for path in ("native_web_search", "native_web_fetch", "perplexity_research", "firecrawl", "x_search_specialist")
            if path in research_paths
        ]

        source_sample: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        domains: list[str] = []
        for source in sources:
            if not isinstance(source, dict):
                continue
            url = str(source.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            title = str(source.get("title") or "").strip()
            domain = str(source.get("domain") or "").strip()
            if domain:
                domains.append(domain)
            source_sample.append(
                {
                    "url": url,
                    "title": title or domain or url,
                    "domain": domain,
                }
            )

        if not ordered_paths and not source_sample:
            return None

        provenance: dict[str, Any] = {}
        if ordered_paths:
            provenance["paths"] = ordered_paths
        if source_sample:
            provenance["source_count"] = len(source_sample)
            provenance["source_domains"] = cls._dedupe_preserve_order(domains)[:3]
            provenance["source_sample"] = source_sample[:3]
        return provenance

    def _record_successful_specialist_usage(self, task_record: dict[str, Any] | None) -> None:
        if not isinstance(task_record, dict):
            return
        if str(task_record.get("status") or "").strip().lower() == "completed":
            return
        recipient = str(task_record.get("recipient") or "").strip()
        intent = str(task_record.get("intent") or "").strip()
        if not recipient or not intent:
            return
        self.registry_store.record_agent_usage(recipient, intent)
        self._refresh_featured_specialists_after_usage()

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

        if tool_name == "agent_catalog_search":
            matches = (data or {}).get("matches") if isinstance(data, dict) else None
            if isinstance(matches, list) and matches:
                labels: list[str] = []
                for item in matches[:2]:
                    if not isinstance(item, dict):
                        continue
                    intent_name = self._activity_excerpt(item.get("intent"), limit=64)
                    agent_label = self._activity_excerpt(item.get("display_name"), limit=64)
                    if intent_name and agent_label:
                        labels.append(f"{intent_name} via {agent_label}")
                    elif intent_name:
                        labels.append(intent_name)
                if labels:
                    return self._format_found_pages_phrase("identified specialist intents", labels)
                return f"identified {len(matches)} specialist intents"
            query = self._activity_excerpt(tool_input.get("query"), limit=72)
            if query:
                return f'searched the specialist catalog for "{query}"'
            return "searched the specialist catalog"

        if tool_name == "delegate_to_agent":
            delegation = (data or {}).get("delegation") if isinstance(data, dict) else None
            intent_name = self._activity_excerpt(
                (delegation or {}).get("intent") or tool_input.get("intent"),
                limit=80,
            )
            agent_label = self._activity_agent_label((delegation or {}).get("agent_id") or tool_input.get("agent_id"))
            phrase_from_message = self._activity_phrase_from_result_message(data)
            if phrase_from_message and intent_name and agent_label:
                return f"delegated {intent_name} to {agent_label} and {phrase_from_message}"
            if phrase_from_message and intent_name:
                return f"delegated {intent_name} and {phrase_from_message}"
            if intent_name and agent_label:
                return f"delegated {intent_name} to {agent_label}"
            if intent_name:
                return f"delegated {intent_name} to a specialist agent"
            return "delegated work to a specialist agent"

        if isinstance(data, dict) and isinstance(data.get("delegation"), dict):
            delegation = data.get("delegation") or {}
            intent_name = self._activity_excerpt(
                delegation.get("intent") or tool_input.get("intent"),
                limit=80,
            )
            agent_label = self._activity_agent_label(delegation.get("agent_id") or tool_input.get("agent_id"))
            phrase_from_message = self._activity_phrase_from_result_message(data)
            if phrase_from_message and intent_name and agent_label:
                return f"used {intent_name} via {agent_label} and {phrase_from_message}"
            if phrase_from_message and intent_name:
                return f"used {intent_name} and {phrase_from_message}"
            if intent_name and agent_label:
                return f"used {intent_name} via {agent_label}"
            if intent_name:
                return f"used {intent_name} via a specialist agent"
            return "used a specialist agent"

        if tool_name == "cosmics_capability_wishlist_search":
            matches = (data or {}).get("matches") if isinstance(data, dict) else None
            query = self._activity_excerpt(tool_input.get("query"), limit=72)
            if isinstance(matches, list) and matches:
                if query:
                    return f'checked COSMIC\'s capability wishlist for "{query}" and found {len(matches)} similar items'
                return f"checked COSMIC's capability wishlist and found {len(matches)} similar items"
            if query:
                return f'checked COSMIC\'s capability wishlist for "{query}"'
            return "checked COSMIC's capability wishlist"

        if tool_name == "cosmics_capability_wishlist_capture":
            status_value = self._activity_excerpt((data or {}).get("status"), limit=48)
            capability_id = self._activity_excerpt((data or {}).get("capability_id"), limit=24)
            title = self._activity_excerpt((data or {}).get("title") or tool_input.get("title"), limit=72)
            if status_value == "created_new" and capability_id and title:
                return f'captured new capability gap {capability_id}: "{title}"'
            if status_value == "updated_existing" and capability_id and title:
                return f'updated capability wishlist entry {capability_id}: "{title}"'
            if status_value == "appended_evidence" and capability_id and title:
                return f'added evidence to capability wishlist entry {capability_id}: "{title}"'
            if status_value == "skipped_duplicate" and capability_id and title:
                return f'reused existing capability wishlist entry {capability_id}: "{title}"'
            return self._activity_phrase_from_result_message(data) or "updated COSMIC's capability wishlist"

        if tool_name == "firecrawl_scrape":
            url = self._activity_url_label((data or {}).get("url") or tool_input.get("url"))
            formats = (data or {}).get("available_formats") if isinstance(data, dict) else None
            if url and isinstance(formats, list) and formats:
                return f'Firecrawl scraped {url} and captured {", ".join(str(item) for item in formats[:3])}'
            if url:
                return f"Firecrawl scraped {url}"
            return "Firecrawl scraped a page"

        if tool_name == "firecrawl_extract":
            urls = (data or {}).get("urls") if isinstance(data, dict) else tool_input.get("urls")
            if isinstance(urls, list):
                cleaned: list[str] = []
                for item in urls:
                    label = self._activity_url_label(item)
                    if label:
                        cleaned.append(label)
                if len(cleaned) == 1:
                    return f"Firecrawl extracted structured data from {cleaned[0]}"
                if cleaned:
                    return f"Firecrawl extracted structured data from {len(cleaned)} pages"
            return "Firecrawl extracted structured data"

        if tool_name == "firecrawl_recall_session":
            session_id = self._activity_excerpt((data or {}).get("session_id") or tool_input.get("session_id"), limit=48)
            entries = (data or {}).get("entries") if isinstance(data, dict) else None
            if session_id and isinstance(entries, list):
                return f"reviewed {len(entries)} prior Firecrawl runs from {session_id}"
            if session_id:
                return f"reviewed prior Firecrawl runs from {session_id}"
            return "reviewed prior Firecrawl runs"

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

    def _activity_agent_label(self, value: Any) -> str | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        normalized = raw
        if "/" in normalized:
            normalized = normalized.split("/", 1)[1]
        if ":" in normalized:
            normalized = normalized.split(":", 1)[0]
        normalized = normalized.replace("-", " ").replace("_", " ").strip()
        return self._activity_excerpt(normalized or raw, limit=72)

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
                        await self._dispatch_resumed_task_for_input_reply(reply)
                        self._pending_input_futures.pop(irid, None)
                        await self._redis.xack(
                            self.config.task_input_replies_stream,
                            self.config.task_input_orchestrator_group,
                            message_id,
                        )
                    except Exception:
                        continue
