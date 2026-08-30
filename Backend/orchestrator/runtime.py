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
import base64
import hashlib
import json
import logging
import mimetypes
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    describe_claims,
    is_supported_image_artifact,
    lookup_model_spec,
)

from .config import BACKEND_ROOT, OrchestratorConfig
from .prompts import build_agentic_system_prompt
from .store.ledger import TaskLedger
from .tools.executor import ToolExecutionContext, ToolExecutor
from .tools.registry import (
    build_tool_progress_message,
    get_local_tool_definitions,
    get_model_tool_definitions,
    get_parallel_safe_local_tool_names,
)
from .visual_enrichment import VisualEnrichmentCoordinator

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


class InlineReasoningSplitter:
    """Separates Fireworks inline <think> content from visible response text."""

    _OPEN_TAG = "<think>"
    _CLOSE_TAG = "</think>"
    _MAX_TAG_LEN = max(len(_OPEN_TAG), len(_CLOSE_TAG))

    def __init__(self) -> None:
        self._buffer = ""
        self._inside_reasoning = False

    def consume(self, chunk: str) -> tuple[str, str]:
        self._buffer += chunk
        return self._drain(flush=False)

    def flush(self) -> tuple[str, str]:
        return self._drain(flush=True)

    def _drain(self, *, flush: bool) -> tuple[str, str]:
        reasoning_parts: list[str] = []
        visible_parts: list[str] = []

        while self._buffer:
            lower = self._buffer.lower()
            if self._inside_reasoning:
                close_index = lower.find(self._CLOSE_TAG)
                if close_index < 0:
                    safe_len = len(self._buffer) if flush else self._safe_emit_len(lower, self._CLOSE_TAG)
                    if safe_len <= 0:
                        break
                    reasoning_parts.append(self._buffer[:safe_len])
                    self._buffer = self._buffer[safe_len:]
                    continue
                reasoning_parts.append(self._buffer[:close_index])
                self._buffer = self._buffer[close_index + len(self._CLOSE_TAG) :]
                self._inside_reasoning = False
                continue

            open_index = lower.find(self._OPEN_TAG)
            if open_index < 0:
                safe_len = len(self._buffer) if flush else self._safe_emit_len(lower, self._OPEN_TAG)
                if safe_len <= 0:
                    break
                visible_parts.append(self._buffer[:safe_len])
                self._buffer = self._buffer[safe_len:]
                continue

            if open_index:
                visible_parts.append(self._buffer[:open_index])
            self._buffer = self._buffer[open_index + len(self._OPEN_TAG) :]
            self._inside_reasoning = True

        return "".join(reasoning_parts), "".join(visible_parts)

    @classmethod
    def _safe_emit_len(cls, lower: str, tag: str) -> int:
        max_prefix = min(len(lower), cls._MAX_TAG_LEN - 1)
        for keep in range(max_prefix, 0, -1):
            if tag.startswith(lower[-keep:]):
                return len(lower) - keep
        return len(lower)


@dataclass(slots=True)
class ActiveTaskRun:
    runner_task: asyncio.Task[Any] | None
    request_id: str | None
    session_id: str | None
    channel: str | None
    cancel_requested: bool = False
    cancel_message: str = "Response stopped."


class OrchestratorTaskError(RuntimeError):
    def __init__(self, message: str, *, code: str, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(slots=True)
class AnthropicRetryPlan:
    message: str
    backoff_sec: float
    model_override: str | None = None


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


@dataclass(slots=True)
class InputArtifactPayload:
    artifact: dict[str, Any]
    filename: str
    mime_type: str
    content: bytes


@dataclass(frozen=True, slots=True)
class OrchestratorModelSelection:
    preferred_provider: str
    preferred_model: str
    effective_provider: str
    effective_model: str
    fallback_reason: str | None = None


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
        self._anthropic_input_file_cache: dict[tuple[str, str, str], str] = {}

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
            artifacts_root=self.config.artifacts_root,
            heartbeat_notes_path=self.config.heartbeat_notes_path,
            local_code_execution_enabled=self.config.local_code_execution_enabled,
            local_code_execution_timeout_sec=self.config.local_code_execution_timeout_sec,
            local_code_execution_allow_network=self.config.local_code_execution_allow_network,
            local_code_execution_allow_pip=self.config.local_code_execution_allow_pip,
            local_code_execution_pip_timeout_sec=self.config.local_code_execution_pip_timeout_sec,
            local_code_execution_venv_cache_root=self.config.local_code_execution_venv_cache_root,
            local_code_execution_max_script_bytes=self.config.local_code_execution_max_script_bytes,
            local_code_execution_max_files=self.config.local_code_execution_max_files,
            local_code_execution_max_file_bytes=self.config.local_code_execution_max_file_bytes,
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

        request_id = str(task.input.get("request_id") or "").strip() or None
        query = str(task.input.get("query") or "").strip()
        session_id = task.session_id
        channel = task.channel
        if not query:
            raise RuntimeError("TaskEnvelope.input.query is required for orchestrator.process")
        model_selection = self._select_initial_orchestrator_model(task)
        orchestrator_provider = model_selection.effective_provider
        if self._is_fireworks_provider(orchestrator_provider):
            if not self.config.fireworks_api_key:
                raise RuntimeError("FIREWORKS_API_KEY is not configured in orchestrator.env.")
        elif not self.config.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured in orchestrator.env.")

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
            "source": task.source,
            "source_id": task.source_id,
            # route is a legacy compatibility token, not an execution lane or model.
            "route": "opus",
            "legacy_route": "opus",
            "dispatch_target": "orchestrator",
            "model_provider": orchestrator_provider,
            "model": model_selection.effective_model,
            "preferred_model_provider": model_selection.preferred_provider,
            "preferred_model": model_selection.preferred_model,
        }

        created_event = {
            **ev,
            "type": "task.created",
            "route": "opus",
            "status": "running",
            "model_provider": orchestrator_provider,
            "model": model_selection.effective_model,
            "preferred_model_provider": model_selection.preferred_provider,
            "preferred_model": model_selection.preferred_model,
        }
        if model_selection.fallback_reason:
            created_event["model_fallback_reason"] = model_selection.fallback_reason
        yield created_event

        if self._is_fireworks_provider(orchestrator_provider):
            async for event in self._stream_fireworks_task(
                task=task,
                ev=ev,
                query=query,
                model_selection=model_selection,
            ):
                yield event
            return

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
            messages = await self._attach_initial_input_artifact_blocks(
                messages,
                task.input_artifacts if isinstance(task.input_artifacts, list) else [],
            )
            self._refresh_featured_specialists()
            visual_mode_enabled = VisualEnrichmentCoordinator.is_enabled_for_task(
                config=self.config,
                task_input=task.input if isinstance(task.input, dict) else {},
            )
            system_prompt = build_agentic_system_prompt(
                str(task.input.get("memory_context") or "").strip() or None,
                user_timezone=str(task.input.get("user_timezone") or "").strip() or None,
                featured_specialists=self._featured_specialists_cache,
                visual_response_enhancement_enabled=visual_mode_enabled,
                visual_supported_slot_kinds=VisualEnrichmentCoordinator.supported_slot_kinds(
                    config=self.config
                )
                if visual_mode_enabled
                else None,
            )
            tools = get_model_tool_definitions(self._featured_specialist_agent_ids())
            max_iterations = self.config.max_tool_iterations

            iteration = 0
            full_response_text = ""
            streamed_visible_text = False
            full_reasoning_text = ""
            collected_sources: list[dict[str, str]] = []
            produced_artifacts: list[dict[str, Any]] = []
            supporting_artifacts: list[dict[str, Any]] = []
            surface_tool_artifacts = task.source != "heartbeat"
            usage_operation = (
                "orchestrator.heartbeat"
                if task.source == "heartbeat"
                else "orchestrator.process"
            )
            research_paths: set[str] = set()
            specialist_receipts: list[dict[str, Any]] = []
            container_id: str | None = None
            container_captured = False
            container_reuse_turns = 0
            anthropic_requests = 0
            effective_anthropic_model = model_selection.effective_model
            max_request_context_chars = 0
            max_request_message_count = 0
            saw_tool_loop = False
            strict_server_tool_replay_recovery = False
            strip_thinking_replay_recovery = False
            visual_coordinator = (
                VisualEnrichmentCoordinator(
                    config=self.config,
                    task_id=task.task_id,
                    request_id=request_id,
                    session_id=session_id,
                    channel=channel,
                    user_query=query,
                    http_client=self._client,
                )
                if visual_mode_enabled
                else None
            )

            while iteration < max_iterations:
                iteration += 1
                turn_stream_boundary_emitted = False
                messages = self._prepare_messages_for_anthropic(
                    messages,
                    strip_all_server_tool_blocks=strict_server_tool_replay_recovery,
                    strip_thinking_blocks=strip_thinking_replay_recovery,
                )
                max_request_context_chars = max(
                    max_request_context_chars,
                    self._estimate_request_context_chars(system_prompt, messages),
                )
                max_request_message_count = max(max_request_message_count, len(messages))

                # ── Stream one Anthropic turn ───────────────────
                turn_overload_retries = 0
                turn_model_override: str | None = None
                turn_fallback_used = False
                blocks: dict[int, ContentBlock] = {}
                turn_usage: dict[str, Any] = {}
                turn_stop_reason: str | None = None

                while True:
                    if container_id:
                        container_reuse_turns += 1
                    anthropic_requests += 1
                    blocks = {}
                    turn_usage = {}
                    turn_stop_reason = None
                    reasoning_announced = False
                    responding_announced = False
                    server_tool_progress_emitted = False
                    effective_anthropic_model = (
                        turn_model_override or model_selection.effective_model
                    )

                    stream_kwargs: dict[str, Any] = {
                        "system_prompt": system_prompt,
                        "messages": messages,
                        "tools": tools,
                        "container_id": container_id,
                        "usage_context": {
                            "task_id": task.task_id,
                            "request_id": request_id,
                            "session_id": session_id,
                            "route": "opus",
                            "operation": usage_operation,
                            "metadata_json": {
                                "legacy_route": "opus",
                                "dispatch_target": "orchestrator",
                                "provider": orchestrator_provider,
                                "model": effective_anthropic_model,
                                "preferred_provider": model_selection.preferred_provider,
                                "preferred_model": model_selection.preferred_model,
                                "iteration": iteration,
                                "source": task.source,
                                "source_id": task.source_id,
                                "channel": channel,
                            },
                        },
                    }
                    if turn_model_override:
                        stream_kwargs["model_override"] = turn_model_override

                    try:
                        async for sse in self._stream_anthropic_events(**stream_kwargs):
                            if sse.event == "ping" or not sse.data:
                                continue
                            payload = json.loads(sse.data)
                            ptype = str(payload.get("type") or "")

                            # ── message_start ───────────────────────────
                            if ptype == "message_start":
                                msg_obj = payload.get("message", {})
                                turn_usage = self._merge_stream_usage(turn_usage, msg_obj.get("usage"))
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
                                turn_usage = self._merge_stream_usage(turn_usage, payload.get("usage"))
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
                                    server_tool_progress_emitted = True
                                    if block.tool_name == "web_search":
                                        progress_msg = "Searching the web..."
                                    elif block.tool_name == "web_fetch":
                                        progress_msg = "Fetching web page..."
                                    elif block.tool_name == "code_execution":
                                        progress_msg = "Running server-side code execution..."
                                    else:
                                        progress_msg = f"Using server-side tool: {block.tool_name}..."
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
                                    chunk, turn_stream_boundary_emitted = self._stream_turn_chunk_delta(
                                        full_response_text,
                                        chunk,
                                        boundary_emitted=turn_stream_boundary_emitted,
                                    )
                                    if not chunk:
                                        continue
                                    if visual_coordinator is not None:
                                        visible_chunk, visual_events = visual_coordinator.consume_text(chunk)
                                        for visual_event in visual_events:
                                            yield {**ev, **visual_event}
                                        if visible_chunk:
                                            streamed_visible_text = True
                                            yield {**ev, "type": "response.chunk", "content": visible_chunk, "done": False}
                                    else:
                                        streamed_visible_text = True
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
                        break
                    except RuntimeError as exc:
                        if (
                            self._is_unmatched_server_tool_replay_error(exc)
                            and not strict_server_tool_replay_recovery
                        ):
                            strict_server_tool_replay_recovery = True
                            messages = self._prepare_messages_for_anthropic(
                                messages,
                                strip_all_server_tool_blocks=True,
                            )
                            logger.warning(
                                "orchestrator.server_tool_replay_recovery task_id=%s request_id=%s iteration=%s reason=%s",
                                task.task_id,
                                request_id,
                                iteration,
                                self._normalize_anthropic_error_text(str(exc)),
                            )
                            yield {
                                **ev,
                                "type": "task.progress",
                                "status": "retrying",
                                "iteration": iteration,
                                "message": "A malformed server-side tool replay was detected. Recovering and retrying...",
                            }
                            continue
                        if (
                            self._is_modified_thinking_replay_error(exc)
                            and not strip_thinking_replay_recovery
                        ):
                            strip_thinking_replay_recovery = True
                            strict_server_tool_replay_recovery = True
                            messages = self._prepare_messages_for_anthropic(
                                messages,
                                strip_all_server_tool_blocks=True,
                                strip_thinking_blocks=True,
                            )
                            logger.warning(
                                "orchestrator.thinking_replay_recovery task_id=%s request_id=%s iteration=%s reason=%s",
                                task.task_id,
                                request_id,
                                iteration,
                                self._normalize_anthropic_error_text(str(exc)),
                            )
                            yield {
                                **ev,
                                "type": "task.progress",
                                "status": "retrying",
                                "iteration": iteration,
                                "message": "A malformed thinking-block replay was detected. Recovering and retrying...",
                            }
                            continue
                        retry_plan = self._plan_anthropic_turn_retry(
                            exc=exc,
                            retry_count=turn_overload_retries,
                            responding_announced=responding_announced,
                            server_tool_progress_emitted=server_tool_progress_emitted,
                            active_model=turn_model_override or self.config.anthropic_model,
                            fallback_used=turn_fallback_used,
                        )
                        if retry_plan is None:
                            raise self._normalize_anthropic_turn_error(exc) from exc
                        logger.warning(
                            "orchestrator.anthropic_retry task_id=%s request_id=%s iteration=%s model=%s retry_count=%s reason=%s",
                            task.task_id,
                            request_id,
                            iteration,
                            turn_model_override or self.config.anthropic_model,
                            turn_overload_retries + 1,
                            self._normalize_anthropic_error_text(str(exc)),
                        )
                        yield {
                            **ev,
                            "type": "task.progress",
                            "status": "retrying",
                            "iteration": iteration,
                            "message": retry_plan.message,
                        }
                        await asyncio.sleep(retry_plan.backoff_sec)
                        turn_overload_retries += 1
                        if retry_plan.model_override:
                            turn_model_override = retry_plan.model_override
                            turn_fallback_used = True
                        continue

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

                if turn_server_blocks and surface_tool_artifacts:
                    await self._collect_server_tool_artifacts(
                        turn_server_blocks,
                        task=task,
                        produced_artifacts=produced_artifacts,
                    )
                if visual_coordinator is not None and collected_sources:
                    for visual_event in visual_coordinator.note_sources(collected_sources):
                        yield {**ev, **visual_event}
                if visual_coordinator is not None and supporting_artifacts:
                    # Screenshots this run captured are candidates for the inline
                    # image slot, not just attachments listed under the answer.
                    for visual_event in visual_coordinator.note_run_images(supporting_artifacts):
                        yield {**ev, **visual_event}

                turn_text = "".join(turn_text_parts)
                turn_reasoning = "".join(turn_reasoning_parts)
                # Pre-tool "Let me..." narration still streams live via response.chunk,
                # but must not accumulate into the final user-facing email/chat body.
                # Otherwise a long tool loop emails process monologue when the turn ends.
                if turn_stop_reason not in {"tool_use", "pause_turn"}:
                    full_response_text = self._append_stream_text(
                        full_response_text,
                        turn_text,
                    )
                full_reasoning_text += turn_reasoning

                # ── Server-side tool continuation (pause_turn) ────
                if turn_stop_reason == "pause_turn":
                    saw_tool_loop = True
                    assistant_content, dropped_server_blocks = self._sanitize_server_tool_replay_blocks(blocks)
                    messages.append({"role": "assistant", "content": assistant_content})
                    loop_message = self._build_server_tool_loop_message(turn_server_blocks)
                    if dropped_server_blocks:
                        loop_message = self._append_server_tool_skip_note(loop_message)
                    yield {
                        **ev, "type": "task.progress",
                        "status": "tool_loop",
                        "iteration": iteration,
                        "message": loop_message,
                    }
                    continue

                # ── Tool use → execute and loop ─────────────────
                if turn_stop_reason == "tool_use" and turn_tool_blocks:
                    saw_tool_loop = True
                    # Reconstruct the assistant message, stripping any incomplete
                    # server-side tool blocks before the next Anthropic request.
                    assistant_content, dropped_server_blocks = self._sanitize_server_tool_replay_blocks(blocks)
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
                        if surface_tool_artifacts:
                            self._collect_specialist_artifacts(
                                result_str,
                                produced_artifacts=produced_artifacts,
                                supporting_artifacts=supporting_artifacts,
                            )
                        self._collect_specialist_receipt(
                            tb.tool_name,
                            pi,
                            result_str,
                            specialist_receipts=specialist_receipts,
                        )
                        self._collect_sandbox_permission_receipt(
                            tb.tool_name,
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
                    if visual_coordinator is not None and collected_sources:
                        for visual_event in visual_coordinator.note_sources(collected_sources):
                            yield {**ev, **visual_event}
                    if visual_coordinator is not None and supporting_artifacts:
                        # Screenshots this run captured are candidates for the
                        # inline image slot, not just attachments under the answer.
                        for visual_event in visual_coordinator.note_run_images(supporting_artifacts):
                            yield {**ev, **visual_event}

                    followup_blocks = (
                        await self._build_tool_result_followup_blocks(result_strs)
                        if surface_tool_artifacts
                        else []
                    )
                    user_content: list[dict[str, Any]] = list(tool_results)
                    if followup_blocks:
                        user_content.extend(followup_blocks)
                    messages.append({"role": "user", "content": user_content})

                    loop_message = self._build_local_tool_loop_message(
                        turn_tool_blocks,
                        parsed_inputs,
                        result_strs,
                        parallel=all_read_only and len(turn_tool_blocks) > 1,
                    )
                    if dropped_server_blocks:
                        loop_message = self._append_server_tool_skip_note(loop_message)
                    yield {
                        **ev, "type": "task.progress",
                        "status": "tool_loop",
                        "iteration": iteration,
                        "tools_called": [tb.tool_name for tb in turn_tool_blocks],
                        "message": loop_message,
                        "specialist_delegations": self._extract_specialist_delegations(
                            turn_tool_blocks,
                            parsed_inputs,
                            result_strs,
                        ),
                    }
                    if streamed_visible_text:
                        async for boundary_event in self._emit_turn_segment_boundary(
                            ev,
                            visual_coordinator=visual_coordinator,
                            iteration=iteration,
                            tools_called=[tb.tool_name for tb in turn_tool_blocks],
                        ):
                            yield boundary_event
                    continue

                # ── Final response (end_turn or other) ──────────
                break

            # ── Emit completion ─────────────────────────────────
            hit_max_iterations = iteration >= max_iterations and stop_reason in ("tool_use", "pause_turn")
            result_type = "max_iterations" if hit_max_iterations else "success"

            final_response_blocks: list[dict[str, Any]] | None = None
            if visual_coordinator is not None:
                final_visual = await visual_coordinator.finalize(
                    produced_artifacts=produced_artifacts,
                )
                for visual_event in final_visual.get("events") or []:
                    if isinstance(visual_event, dict):
                        yield {**ev, **visual_event}
                for item in (final_visual.get("supporting_artifacts") or []):
                    if not isinstance(item, dict):
                        continue
                    dedupe_key = (
                        str(item.get("artifact_id") or "").strip(),
                        str(item.get("path") or "").strip(),
                    )
                    if not any(dedupe_key):
                        continue
                    existing_keys = {
                        (
                            str(existing.get("artifact_id") or "").strip(),
                            str(existing.get("path") or "").strip(),
                        )
                        for existing in supporting_artifacts
                        if isinstance(existing, dict)
                    }
                    if dedupe_key not in existing_keys:
                        supporting_artifacts.append(item)
                display_text = str(final_visual.get("content") or "").rstrip()
                final_response_blocks = (
                    final_visual.get("response_blocks")
                    if isinstance(final_visual.get("response_blocks"), list)
                    else None
                )
            else:
                display_text = full_response_text.rstrip()
            awaiting_reply = display_text.endswith(AWAITING_REPLY_TAG)
            if awaiting_reply:
                display_text = display_text.removesuffix(AWAITING_REPLY_TAG).rstrip()
                if final_response_blocks:
                    for block in reversed(final_response_blocks):
                        if str(block.get("type")) != "markdown":
                            continue
                        text = str(block.get("text") or "")
                        if text.endswith(AWAITING_REPLY_TAG):
                            block["text"] = text.removesuffix(AWAITING_REPLY_TAG).rstrip()
                        break

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
                "model_provider": orchestrator_provider,
                "model": effective_anthropic_model,
                "preferred_model_provider": model_selection.preferred_provider,
                "preferred_model": model_selection.preferred_model,
                "metrics": {
                    "rtt_ms": elapsed_ms,
                    "tool_iterations": iteration,
                    "anthropic_requests": anthropic_requests,
                    "container_captured": container_captured,
                    "container_reuse_turns": container_reuse_turns,
                    "model_provider": orchestrator_provider,
                    "model": effective_anthropic_model,
                    "preferred_model_provider": model_selection.preferred_provider,
                    "preferred_model": model_selection.preferred_model,
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
            if supporting_artifacts:
                complete_event["supporting_artifacts"] = supporting_artifacts
            if final_response_blocks:
                complete_event["response_blocks"] = final_response_blocks
            yield complete_event
            yield {
                **ev,
                "type": "task.completed",
                "route": "opus",
                "status": "completed",
                "model_provider": orchestrator_provider,
                "model": effective_anthropic_model,
                "preferred_model_provider": model_selection.preferred_provider,
                "preferred_model": model_selection.preferred_model,
            }

        except asyncio.CancelledError:
            run_state = self._active_runs.get(task.task_id)
            if run_state and run_state.cancel_requested:
                message = run_state.cancel_message
                self.task_ledger.mark_cancelled(task.task_id, message=message)
                yield {
                    **ev,
                    "type": "task.cancelled",
                    "route": "opus",
                    "status": "cancelled",
                    "message": message,
                    "model_provider": orchestrator_provider,
                    "model": locals().get(
                        "effective_anthropic_model",
                        model_selection.effective_model,
                    ),
                    "preferred_model_provider": model_selection.preferred_provider,
                    "preferred_model": model_selection.preferred_model,
                }
                return
            self.task_ledger.mark_failed(
                task.task_id,
                code="STREAM_DISCONNECTED",
                message="The upstream stream ended before the task completed.",
            )
            raise
        except Exception as exc:
            if isinstance(exc, OrchestratorTaskError):
                code = exc.code
                message = str(exc).strip() or "Orchestrator processing failed."
                retryable = exc.retryable
            else:
                code = "OPUS_UPSTREAM_ERROR"
                message = str(exc).strip() or "Orchestrator processing failed."
                retryable = False
            self.task_ledger.mark_failed(task.task_id, code=code, message=message)
            yield {
                **ev, "type": "task.failed", "route": "opus", "status": "failed",
                "model_provider": orchestrator_provider,
                "model": locals().get(
                    "effective_anthropic_model",
                    model_selection.effective_model,
                ),
                "preferred_model_provider": model_selection.preferred_provider,
                "preferred_model": model_selection.preferred_model,
                "error": {"code": code, "message": message, "retryable": retryable},
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

    async def _stream_fireworks_task(
        self,
        *,
        task: TaskEnvelope,
        ev: dict[str, Any],
        query: str,
        model_selection: OrchestratorModelSelection,
    ) -> AsyncIterator[dict[str, Any]]:
        request_id = str(ev.get("request_id") or "").strip() or None
        session_id = task.session_id
        channel = task.channel
        started_at = time.perf_counter()
        cumulative_usage: dict[str, int] = {}
        stop_reason: str | None = None
        iteration = 0
        fireworks_requests = 0
        max_request_context_chars = 0
        max_request_message_count = 0
        full_response_text = ""
        streamed_visible_text = False
        full_reasoning_text = ""
        collected_sources: list[dict[str, str]] = []
        produced_artifacts: list[dict[str, Any]] = []
        supporting_artifacts: list[dict[str, Any]] = []
        surface_tool_artifacts = task.source != "heartbeat"
        usage_operation = (
            "orchestrator.heartbeat"
            if task.source == "heartbeat"
            else "orchestrator.process"
        )
        research_paths: set[str] = set()
        specialist_receipts: list[dict[str, Any]] = []
        preferred_provider = model_selection.preferred_provider
        preferred_model = model_selection.preferred_model
        effective_provider = model_selection.effective_provider
        effective_model = model_selection.effective_model
        effective_model_keys: set[str] = {f"{effective_provider}:{effective_model}"}
        last_announced_selection_key = f"{effective_provider}:{effective_model}"

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
            visual_mode_enabled = VisualEnrichmentCoordinator.is_enabled_for_task(
                config=self.config,
                task_input=task.input if isinstance(task.input, dict) else {},
            )
            system_prompt = build_agentic_system_prompt(
                str(task.input.get("memory_context") or "").strip() or None,
                user_timezone=str(task.input.get("user_timezone") or "").strip() or None,
                featured_specialists=self._featured_specialists_cache,
                visual_response_enhancement_enabled=visual_mode_enabled,
                visual_supported_slot_kinds=VisualEnrichmentCoordinator.supported_slot_kinds(
                    config=self.config
                )
                if visual_mode_enabled
                else None,
            )
            system_prompt = self._with_fireworks_runtime_note(system_prompt)
            openai_messages = [
                {"role": "system", "content": system_prompt},
                *self._messages_to_openai_chat(messages),
            ]
            tools = self._tools_to_openai_chat(get_local_tool_definitions(self._featured_specialist_agent_ids()))
            max_iterations = self.config.max_tool_iterations
            visual_coordinator = (
                VisualEnrichmentCoordinator(
                    config=self.config,
                    task_id=task.task_id,
                    request_id=request_id,
                    session_id=session_id,
                    channel=channel,
                    user_query=query,
                    http_client=self._client,
                )
                if visual_mode_enabled
                else None
            )

            while iteration < max_iterations:
                iteration += 1
                turn_stream_boundary_emitted = False
                fireworks_requests += 1
                turn_selection = self._effective_fireworks_selection_for_images(
                    preferred_provider=preferred_provider,
                    preferred_model=preferred_model,
                    has_images=self._openai_messages_have_image_input(openai_messages),
                )
                effective_provider = turn_selection.effective_provider
                effective_model = turn_selection.effective_model
                effective_model_keys.add(f"{effective_provider}:{effective_model}")
                selection_key = f"{effective_provider}:{effective_model}"
                if (
                    selection_key != last_announced_selection_key
                    and turn_selection.fallback_reason == "image_input"
                ):
                    yield {
                        **ev,
                        "type": "task.progress",
                        "status": "model_switch",
                        "message": "Cosmic switched to Kimi for visual input.",
                        "model_provider": effective_provider,
                        "model": effective_model,
                        "preferred_model_provider": preferred_provider,
                        "preferred_model": preferred_model,
                        "reason": turn_selection.fallback_reason,
                    }
                last_announced_selection_key = selection_key
                max_request_context_chars = max(
                    max_request_context_chars,
                    self._estimate_openai_request_context_chars(openai_messages),
                )
                max_request_message_count = max(max_request_message_count, len(openai_messages))

                turn_text_parts: list[str] = []
                turn_reasoning_parts: list[str] = []
                turn_tool_calls: dict[int, dict[str, Any]] = {}
                turn_usage: dict[str, int] = {}
                turn_finish_reason: str | None = None
                reasoning_announced = False
                responding_announced = False
                inline_reasoning_splitter = InlineReasoningSplitter()

                def collect_reasoning_events(reasoning_text: str) -> list[dict[str, Any]]:
                    nonlocal reasoning_announced
                    if not reasoning_text:
                        return []
                    turn_reasoning_parts.append(reasoning_text)
                    events: list[dict[str, Any]] = []
                    if not reasoning_announced:
                        reasoning_announced = True
                        events.append(
                            {
                                **ev,
                                "type": "task.progress",
                                "status": "thinking",
                                "message": "Cosmic is reasoning through the request.",
                                "model_provider": effective_provider,
                                "model": effective_model,
                                "preferred_model_provider": preferred_provider,
                                "preferred_model": preferred_model,
                            }
                        )
                    if iteration == 1:
                        events.append(
                            {
                                **ev,
                                "type": "response.thinking.chunk",
                                "content": reasoning_text,
                                "done": False,
                            }
                        )
                    return events

                def collect_content_events(visible_text: str) -> list[dict[str, Any]]:
                    nonlocal responding_announced, turn_stream_boundary_emitted
                    nonlocal streamed_visible_text
                    if not visible_text:
                        return []
                    chunk_to_emit = visible_text
                    chunk_to_emit, turn_stream_boundary_emitted = self._stream_turn_chunk_delta(
                        full_response_text,
                        visible_text,
                        boundary_emitted=turn_stream_boundary_emitted,
                    )
                    if not chunk_to_emit:
                        return []
                    turn_text_parts.append(visible_text)
                    events: list[dict[str, Any]] = []
                    if not responding_announced:
                        responding_announced = True
                        events.append(
                            {
                                **ev,
                                "type": "task.progress",
                                "status": "responding",
                                "message": "Cosmic is writing the response.",
                                "model_provider": effective_provider,
                                "model": effective_model,
                                "preferred_model_provider": preferred_provider,
                                "preferred_model": preferred_model,
                            }
                        )
                    if visual_coordinator is not None:
                        visible_chunk, visual_events = visual_coordinator.consume_text(chunk_to_emit)
                        for visual_event in visual_events:
                            events.append({**ev, **visual_event})
                        if visible_chunk:
                            streamed_visible_text = True
                            events.append(
                                {
                                    **ev,
                                    "type": "response.chunk",
                                    "content": visible_chunk,
                                    "done": False,
                                }
                            )
                    else:
                        streamed_visible_text = True
                        events.append(
                            {
                                **ev,
                                "type": "response.chunk",
                                "content": chunk_to_emit,
                                "done": False,
                            }
                        )
                    return events

                async for payload in self._stream_openai_chat_events(
                    model_name=effective_model,
                    messages=openai_messages,
                    tools=tools,
                    usage_context={
                        "task_id": task.task_id,
                        "request_id": request_id,
                        "session_id": session_id,
                        "route": "opus",
                        "operation": usage_operation,
                        "metadata_json": {
                            "legacy_route": "opus",
                            "dispatch_target": "orchestrator",
                            "provider": effective_provider,
                            "model": effective_model,
                            "preferred_provider": preferred_provider,
                            "preferred_model": preferred_model,
                            "iteration": iteration,
                            "source": task.source,
                            "source_id": task.source_id,
                            "channel": channel,
                        },
                    },
                ):
                    usage_batch = self._extract_openai_usage(payload)
                    if usage_batch:
                        turn_usage = self._merge_stream_usage(turn_usage, usage_batch)
                    choices = payload.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    first = choices[0]
                    if not isinstance(first, dict):
                        continue
                    finish_reason = str(first.get("finish_reason") or "").strip()
                    if finish_reason:
                        turn_finish_reason = finish_reason
                    delta = first.get("delta")
                    if not isinstance(delta, dict):
                        continue

                    reasoning_chunk = str(
                        delta.get("reasoning_content")
                        or delta.get("reasoning")
                        or ""
                    )
                    if reasoning_chunk:
                        for event in collect_reasoning_events(reasoning_chunk):
                            yield event

                    content_chunk = str(delta.get("content") or "")
                    if content_chunk:
                        inline_reasoning, visible_content = inline_reasoning_splitter.consume(content_chunk)
                        for event in collect_reasoning_events(inline_reasoning):
                            yield event
                        for event in collect_content_events(visible_content):
                            yield event

                    for tool_call_delta in self._extract_openai_tool_call_deltas(delta):
                        index = int(tool_call_delta.get("index", 0))
                        state = turn_tool_calls.setdefault(
                            index,
                            {
                                "id": "",
                                "name": "",
                                "arguments": "",
                            },
                        )
                        if tool_call_delta.get("id"):
                            state["id"] = str(tool_call_delta.get("id") or "")
                        function_delta = tool_call_delta.get("function")
                        if isinstance(function_delta, dict):
                            if function_delta.get("name"):
                                state["name"] = str(function_delta.get("name") or "")
                            if function_delta.get("arguments"):
                                state["arguments"] = (
                                    str(state.get("arguments") or "")
                                    + str(function_delta.get("arguments") or "")
                                )

                inline_reasoning, visible_content = inline_reasoning_splitter.flush()
                for event in collect_reasoning_events(inline_reasoning):
                    yield event
                for event in collect_content_events(visible_content):
                    yield event

                cumulative_usage = self._merge_usage(cumulative_usage, turn_usage)
                stop_reason = turn_finish_reason
                turn_text = "".join(turn_text_parts)
                turn_reasoning = "".join(turn_reasoning_parts)
                normalized_tool_calls = self._normalize_openai_tool_calls(turn_tool_calls)
                # Streamed pre-tool narration stays live; only final end-turn text
                # becomes the persisted/emailable response body.
                if not normalized_tool_calls:
                    full_response_text = self._append_stream_text(full_response_text, turn_text)
                full_reasoning_text += turn_reasoning

                if normalized_tool_calls:
                    assistant_message: dict[str, Any] = {
                        "role": "assistant",
                        "content": turn_text or None,
                        "tool_calls": [
                            {
                                "id": item["id"],
                                "type": "function",
                                "function": {
                                    "name": item["name"],
                                    "arguments": item["arguments"],
                                },
                            }
                            for item in normalized_tool_calls
                        ],
                    }
                    openai_messages.append(assistant_message)

                    parsed_inputs: list[dict[str, Any]] = []
                    for item in normalized_tool_calls:
                        try:
                            parsed_input = json.loads(item["arguments"]) if item["arguments"] else {}
                        except json.JSONDecodeError:
                            parsed_input = {}
                        if not isinstance(parsed_input, dict):
                            parsed_input = {}
                        parsed_inputs.append(parsed_input)

                        progress_msg = build_tool_progress_message(item["name"], parsed_input)
                        yield {
                            **ev,
                            "type": "task.progress",
                            "status": "tool_call",
                            "iteration": iteration,
                            "tool_name": item["name"],
                            "message": progress_msg,
                        }
                        yield {
                            **ev,
                            "type": "tool.call",
                            "iteration": iteration,
                            "tool_name": item["name"],
                            "tool_call_id": item["id"],
                            "tool_input": parsed_input,
                        }

                    run_state = self._active_runs.get(task.task_id)
                    if run_state and run_state.cancel_requested:
                        raise asyncio.CancelledError()

                    assert self._tool_executor is not None
                    all_read_only = all(
                        item["name"] in _PARALLEL_SAFE_TOOLS
                        for item in normalized_tool_calls
                    )
                    if all_read_only and len(normalized_tool_calls) > 1:
                        result_strs = list(await asyncio.gather(*(
                            self._tool_executor.execute(item["name"], parsed_input, context=tool_context)
                            for item, parsed_input in zip(normalized_tool_calls, parsed_inputs)
                        )))
                    else:
                        result_strs = []
                        for item, parsed_input in zip(normalized_tool_calls, parsed_inputs):
                            result_strs.append(
                                await self._tool_executor.execute(
                                    item["name"],
                                    parsed_input,
                                    context=tool_context,
                                )
                            )

                    for item, parsed_input, result_str in zip(normalized_tool_calls, parsed_inputs, result_strs):
                        tool_name = item["name"]
                        if tool_name == "perplexity_research":
                            research_paths.add("perplexity_research")
                            self._collect_perplexity_sources(result_str, collected_sources)
                        elif tool_name in {"firecrawl_scrape", "firecrawl_extract", "firecrawl_recall_session"}:
                            research_paths.add("firecrawl")
                        elif tool_name in {"x_search", "x_recall_session"}:
                            research_paths.add("x_search_specialist")
                            self._collect_x_specialist_sources(result_str, collected_sources)
                        elif tool_name == "delegate_to_agent":
                            self._inherit_specialist_research_provenance(
                                result_str,
                                research_paths=research_paths,
                                sources=collected_sources,
                            )
                        if surface_tool_artifacts:
                            self._collect_specialist_artifacts(
                                result_str,
                                produced_artifacts=produced_artifacts,
                                supporting_artifacts=supporting_artifacts,
                            )
                        self._collect_specialist_receipt(
                            tool_name,
                            parsed_input,
                            result_str,
                            specialist_receipts=specialist_receipts,
                        )
                        self._collect_sandbox_permission_receipt(
                            tool_name,
                            result_str,
                            specialist_receipts=specialist_receipts,
                        )
                        yield {
                            **ev,
                            "type": "tool.result",
                            "iteration": iteration,
                            "tool_name": tool_name,
                            "tool_call_id": item["id"],
                            "result_preview": result_str[:500],
                        }
                        openai_messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": item["id"],
                                "name": tool_name,
                                "content": result_str,
                            }
                        )
                    if visual_coordinator is not None and collected_sources:
                        for visual_event in visual_coordinator.note_sources(collected_sources):
                            yield {**ev, **visual_event}
                    if visual_coordinator is not None and supporting_artifacts:
                        # Screenshots this run captured are candidates for the
                        # inline image slot, not just attachments under the answer.
                        for visual_event in visual_coordinator.note_run_images(supporting_artifacts):
                            yield {**ev, **visual_event}

                    followup_content = (
                        await self._build_openai_tool_result_followup_content(result_strs)
                        if surface_tool_artifacts
                        else ""
                    )
                    if followup_content:
                        openai_messages.append({"role": "user", "content": followup_content})

                    loop_message = self._build_openai_tool_loop_message(
                        normalized_tool_calls,
                        parsed_inputs,
                        result_strs,
                        parallel=all_read_only and len(normalized_tool_calls) > 1,
                    )
                    yield {
                        **ev,
                        "type": "task.progress",
                        "status": "tool_loop",
                        "iteration": iteration,
                        "tools_called": [item["name"] for item in normalized_tool_calls],
                        "message": loop_message,
                        "specialist_delegations": self._extract_specialist_delegations_from_names(
                            normalized_tool_calls,
                            parsed_inputs,
                            result_strs,
                        ),
                    }
                    if streamed_visible_text:
                        async for boundary_event in self._emit_turn_segment_boundary(
                            ev,
                            visual_coordinator=visual_coordinator,
                            iteration=iteration,
                            tools_called=[item["name"] for item in normalized_tool_calls],
                        ):
                            yield boundary_event
                    continue

                break

            hit_max_iterations = iteration >= max_iterations and bool(stop_reason == "tool_calls")
            result_type = "max_iterations" if hit_max_iterations else "success"

            if hit_max_iterations and not full_response_text.strip():
                openai_messages.append(
                    {
                        "role": "user",
                        "content": (
                            "You are out of tool calls for this turn and have not "
                            "replied yet. Do not call any more tools. Respond "
                            "directly now: summarize what you found, what you "
                            "tried, what is still unresolved, and what you need "
                            "from the user (such as a direct link or account "
                            "access) to finish, if anything."
                        ),
                    }
                )
                try:
                    finalize_text, finalize_usage = await self._finalize_openai_text_without_tools(
                        model_name=effective_model,
                        messages=openai_messages,
                        usage_context={
                            "task_id": task.task_id,
                            "request_id": request_id,
                            "session_id": session_id,
                            "route": "opus",
                            "operation": usage_operation,
                            "metadata_json": {
                                "legacy_route": "opus",
                                "dispatch_target": "orchestrator",
                                "provider": effective_provider,
                                "model": effective_model,
                                "preferred_provider": preferred_provider,
                                "preferred_model": preferred_model,
                                "iteration": iteration,
                                "source": task.source,
                                "source_id": task.source_id,
                                "channel": channel,
                                "finalization": True,
                            },
                        },
                    )
                except Exception:
                    logger.exception(
                        "orchestrator.max_iterations_finalization_failed task_id=%s",
                        task.task_id,
                    )
                    finalize_text = ""
                    finalize_usage = {}
                if finalize_text:
                    for event in collect_content_events(finalize_text):
                        yield event
                    full_response_text = self._append_stream_text(full_response_text, finalize_text)
                    cumulative_usage = self._merge_usage(cumulative_usage, finalize_usage)
                    stop_reason = "end_turn"
                    result_type = "max_iterations_finalized"

            final_response_blocks: list[dict[str, Any]] | None = None
            if visual_coordinator is not None:
                final_visual = await visual_coordinator.finalize(
                    produced_artifacts=produced_artifacts,
                )
                for visual_event in final_visual.get("events") or []:
                    if isinstance(visual_event, dict):
                        yield {**ev, **visual_event}
                for item in (final_visual.get("supporting_artifacts") or []):
                    if not isinstance(item, dict):
                        continue
                    dedupe_key = (
                        str(item.get("artifact_id") or "").strip(),
                        str(item.get("path") or "").strip(),
                    )
                    if not any(dedupe_key):
                        continue
                    existing_keys = {
                        (
                            str(existing.get("artifact_id") or "").strip(),
                            str(existing.get("path") or "").strip(),
                        )
                        for existing in supporting_artifacts
                        if isinstance(existing, dict)
                    }
                    if dedupe_key not in existing_keys:
                        supporting_artifacts.append(item)
                display_text = str(final_visual.get("content") or "").rstrip()
                final_response_blocks = (
                    final_visual.get("response_blocks")
                    if isinstance(final_visual.get("response_blocks"), list)
                    else None
                )
            else:
                display_text = full_response_text.rstrip()
            awaiting_reply = display_text.endswith(AWAITING_REPLY_TAG)
            if awaiting_reply:
                display_text = display_text.removesuffix(AWAITING_REPLY_TAG).rstrip()
                if final_response_blocks:
                    for block in reversed(final_response_blocks):
                        if str(block.get("type")) != "markdown":
                            continue
                        text = str(block.get("text") or "")
                        if text.endswith(AWAITING_REPLY_TAG):
                            block["text"] = text.removesuffix(AWAITING_REPLY_TAG).rstrip()
                        break

            result_payload = {
                "content": display_text,
                "thinking_text": full_reasoning_text,
                "awaiting_reply": awaiting_reply,
                "usage": cumulative_usage,
                "stop_reason": stop_reason,
                "result_type": result_type,
                "tool_iterations": iteration,
                "loop_diagnostics": {
                    "fireworks_requests": fireworks_requests,
                    "model_provider": effective_provider,
                    "model": effective_model,
                    "preferred_model_provider": preferred_provider,
                    "preferred_model": preferred_model,
                    "effective_models": sorted(effective_model_keys),
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
                "model_provider": effective_provider,
                "model": effective_model,
                "preferred_model_provider": preferred_provider,
                "preferred_model": preferred_model,
                "metrics": {
                    "rtt_ms": elapsed_ms,
                    "tool_iterations": iteration,
                    "fireworks_requests": fireworks_requests,
                    "model_provider": effective_provider,
                    "model": effective_model,
                    "preferred_model_provider": preferred_provider,
                    "preferred_model": preferred_model,
                    "effective_models": sorted(effective_model_keys),
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
            if supporting_artifacts:
                complete_event["supporting_artifacts"] = supporting_artifacts
            if final_response_blocks:
                complete_event["response_blocks"] = final_response_blocks
            yield complete_event
            yield {
                **ev,
                "type": "task.completed",
                "route": "opus",
                "status": "completed",
                "model_provider": effective_provider,
                "model": effective_model,
                "preferred_model_provider": preferred_provider,
                "preferred_model": preferred_model,
            }

        except asyncio.CancelledError:
            run_state = self._active_runs.get(task.task_id)
            if run_state and run_state.cancel_requested:
                message = run_state.cancel_message
                self.task_ledger.mark_cancelled(task.task_id, message=message)
                yield {
                    **ev,
                    "type": "task.cancelled",
                    "route": "opus",
                    "status": "cancelled",
                    "message": message,
                    "model_provider": effective_provider,
                    "model": effective_model,
                    "preferred_model_provider": preferred_provider,
                    "preferred_model": preferred_model,
                }
                return
            self.task_ledger.mark_failed(
                task.task_id,
                code="STREAM_DISCONNECTED",
                message="The upstream stream ended before the task completed.",
            )
            raise
        except Exception as exc:
            code = "FIREWORKS_UPSTREAM_ERROR"
            message = str(exc).strip() or "Fireworks orchestrator processing failed."
            self.task_ledger.mark_failed(task.task_id, code=code, message=message)
            yield {
                **ev,
                "type": "task.failed",
                "route": "opus",
                "status": "failed",
                "model_provider": effective_provider,
                "model": effective_model,
                "preferred_model_provider": preferred_provider,
                "preferred_model": preferred_model,
                "error": {"code": code, "message": message, "retryable": False},
            }
        finally:
            self._active_runs.pop(task.task_id, None)

    # ════════════════════════════════════════════════════════════
    #  Task management
    # ════════════════════════════════════════════════════════════

    def list_active_tasks(self, *, session_id: str | None = None, channel: str | None = None) -> list[dict[str, Any]]:
        return self.task_ledger.list_active_tasks(session_id=session_id, channel=channel)

    async def cancel_task(self, task_id: str, *, message: str = "Response stopped.") -> bool:
        tid = str(task_id or "").strip()
        if not tid:
            return False
        cancelled_any = False
        run_state = self._active_runs.get(tid)
        if run_state is not None:
            run_state.cancel_requested = True
            run_state.cancel_message = message
            runner = run_state.runner_task
            if runner is not None and not runner.done():
                runner.cancel()
            cancelled_any = True

        task_record = self.task_ledger.get_task(tid)
        if task_record is not None:
            status = str(task_record.get("status") or "").strip()
            if status in {"running", "suspended", "deferred"}:
                self.task_ledger.mark_cancelled(tid, message=message)
                await self._request_agent_task_cancel(tid)
                self._resolve_pending_agent_result(
                    tid,
                    AgentResult(
                        status="failed",
                        output={},
                        artifacts=[],
                        error=AgentError(
                            code="CANCELLED",
                            retryable=False,
                            message=message,
                            next_action="skip",
                        ),
                    ),
                )
                cancelled_any = True
            for child in self.task_ledger.list_active_descendant_tasks(tid):
                child_id = str(child.get("task_id") or "").strip()
                if not child_id:
                    continue
                self.task_ledger.mark_cancelled(child_id, message=message)
                await self._request_agent_task_cancel(child_id)
                self._resolve_pending_agent_result(
                    child_id,
                    AgentResult(
                        status="failed",
                        output={},
                        artifacts=[],
                        error=AgentError(
                            code="CANCELLED",
                            retryable=False,
                            message=message,
                            next_action="skip",
                        ),
                    ),
                )
                cancelled_any = True
        return cancelled_any

    async def _request_agent_task_cancel(self, task_id: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.set(f"task_cancel:{task_id}", "1", ex=86_400)
        except Exception:
            logger.exception("orchestrator.task_cancel_signal_failed task_id=%s", task_id)

    def get_loop_diagnostics_snapshot(self) -> dict[str, int]:
        return self._anthropic_loop_stats.as_dict()

    def _select_initial_orchestrator_model(self, task: TaskEnvelope) -> OrchestratorModelSelection:
        preferred_provider, preferred_model = self._select_preferred_orchestrator_model(task)
        if self._is_fireworks_provider(preferred_provider):
            return self._effective_fireworks_selection_for_images(
                preferred_provider=preferred_provider,
                preferred_model=preferred_model,
                has_images=self._task_has_image_input(task),
            )
        return OrchestratorModelSelection(
            preferred_provider="anthropic",
            preferred_model=preferred_model,
            effective_provider="anthropic",
            effective_model=preferred_model,
        )

    def _select_preferred_orchestrator_model(self, task: TaskEnvelope) -> tuple[str, str]:
        task_input = task.input if isinstance(task.input, dict) else {}
        raw_preference = task_input.get("cosmic_orchestrator_model")
        if not isinstance(raw_preference, dict):
            gateway_preferences = task_input.get("gateway_preferences")
            if isinstance(gateway_preferences, dict):
                raw_preference = gateway_preferences.get("cosmic_orchestrator_model")
        if isinstance(raw_preference, dict):
            provider = self._normalize_orchestrator_provider(raw_preference.get("provider"))
            if provider:
                model = str(raw_preference.get("model") or "").strip()
                return provider, model or self._default_model_for_orchestrator_provider(provider)
        provider = self._normalize_orchestrator_provider(self.config.orchestrator_default_provider) or "anthropic"
        return provider, self._default_model_for_orchestrator_provider(provider)

    def _default_model_for_orchestrator_provider(self, provider: str) -> str:
        if provider == "fireworks_kimi":
            return self.config.fireworks_kimi_model
        if provider == "fireworks_glm":
            return self.config.fireworks_glm_model
        return self.config.anthropic_model

    @staticmethod
    def _is_fireworks_provider(provider: str) -> bool:
        return provider in {"fireworks_kimi", "fireworks_glm"}

    def _effective_fireworks_selection_for_images(
        self,
        *,
        preferred_provider: str,
        preferred_model: str,
        has_images: bool,
    ) -> OrchestratorModelSelection:
        normalized_provider = (
            preferred_provider
            if self._is_fireworks_provider(preferred_provider)
            else self._fireworks_provider_for_model(preferred_model)
        )
        normalized_model = preferred_model or self._default_model_for_orchestrator_provider(normalized_provider)
        if not has_images or self._fireworks_model_supports_image_input(normalized_model):
            return OrchestratorModelSelection(
                preferred_provider=normalized_provider,
                preferred_model=normalized_model,
                effective_provider=normalized_provider,
                effective_model=normalized_model,
            )
        fallback_model = (
            self.config.fireworks_vision_fallback_model
            or self.config.fireworks_kimi_model
            or "accounts/fireworks/models/kimi-k2p6"
        )
        if not self._fireworks_model_supports_image_input(fallback_model):
            fallback_model = "accounts/fireworks/models/kimi-k2p6"
        return OrchestratorModelSelection(
            preferred_provider=normalized_provider,
            preferred_model=normalized_model,
            effective_provider=self._fireworks_provider_for_model(fallback_model),
            effective_model=fallback_model,
            fallback_reason="image_input",
        )

    @staticmethod
    def _fireworks_provider_for_model(model: str) -> str:
        normalized = str(model or "").strip().lower()
        if "glm" in normalized:
            return "fireworks_glm"
        return "fireworks_kimi"

    @staticmethod
    def _fireworks_model_supports_image_input(model: str) -> bool:
        normalized_model = str(model or "").strip()
        spec = lookup_model_spec("fireworks", normalized_model)
        if spec is not None:
            return bool(spec.capabilities.get("supports_image_input"))
        # Unknown Fireworks models should not be assumed vision-capable, except
        # for Kimi-family fallback names used before specs are added.
        return "kimi" in normalized_model.lower()

    @staticmethod
    def _task_has_image_input(task: TaskEnvelope) -> bool:
        artifacts = task.input_artifacts if isinstance(task.input_artifacts, list) else []
        for artifact in artifacts:
            if isinstance(artifact, dict) and is_supported_image_artifact(artifact):
                if str(artifact.get("provider_url") or "").strip():
                    return True
        task_input = task.input if isinstance(task.input, dict) else {}
        context = task_input.get("conversation_context")
        if isinstance(context, list):
            for item in context:
                if isinstance(item, dict) and OrchestratorRuntime._anthropic_content_has_image_input(item.get("content")):
                    return True
        return False

    @classmethod
    def _anthropic_content_has_image_input(cls, content: Any) -> bool:
        if isinstance(content, list):
            return any(cls._anthropic_content_has_image_input(item) for item in content)
        if isinstance(content, dict):
            part_type = str(content.get("type") or "").strip()
            if part_type == "image":
                return True
            source = content.get("source") if isinstance(content.get("source"), dict) else {}
            if str(source.get("type") or "").strip() in {"url", "base64"}:
                return True
            return any(cls._anthropic_content_has_image_input(value) for value in content.values())
        return False

    @classmethod
    def _openai_messages_have_image_input(cls, messages: list[dict[str, Any]]) -> bool:
        for message in messages:
            if not isinstance(message, dict):
                continue
            if cls._openai_content_has_image_input(message.get("content")):
                return True
        return False

    @classmethod
    def _openai_content_has_image_input(cls, content: Any) -> bool:
        if isinstance(content, list):
            return any(cls._openai_content_has_image_input(item) for item in content)
        if isinstance(content, dict):
            part_type = str(content.get("type") or "").strip()
            if part_type in {"image_url", "image"}:
                return True
            if isinstance(content.get("image_url"), dict):
                return True
            return any(cls._openai_content_has_image_input(value) for value in content.values())
        return False

    @staticmethod
    def _normalize_orchestrator_provider(value: Any) -> str | None:
        normalized = str(value or "").strip().lower().replace("-", "_")
        if normalized in {"", "auto", "default", "cosmic"}:
            return None
        if normalized in {"fireworks", "fireworks_kimi", "kimi", "kimi_k2_6", "smarter"}:
            return "fireworks_kimi"
        if normalized in {"fireworks_glm", "glm", "glm_5p2", "glm_5_2", "glm52"}:
            return "fireworks_glm"
        if normalized in {"anthropic", "claude", "opus", "sonnet"}:
            return "anthropic"
        return "anthropic"

    @staticmethod
    def _with_fireworks_runtime_note(system_prompt: str) -> str:
        note = (
            "## COSMIC Runtime Provider\n"
            "You are running on COSMIC's Fireworks OpenAI-compatible orchestrator path. "
            "The preferred model may be GLM 5.2 or Kimi; COSMIC will automatically scope a Kimi fallback for any turn or follow-up that needs direct visual input when the preferred model lacks image support. "
            "Anthropic's hosted code_execution container and native server web tools are not attached on this path, but COSMIC provides `cosmic_code_execution` as a bounded local Python sandbox. "
            "Use `cosmic_code_execution` for calculations, small Python checks, data transforms, chart/file generation, and artifact-producing snippets; write deliverable files under `outputs/` so COSMIC can attach them. "
            "Sandbox network access and VM file reads (via `requested_capabilities`) run automatically with no approval — do not ask the user to allow them first. For VM file edits/writes, call the tool and stop: the inline Allow/Deny card appears automatically, and once the user approves, the sandbox runs and your turn resumes with the result (do not re-issue the call). "
            "Do not use `cosmic_code_execution` to generate maps, route visuals, geocoding displays, or Folium/HTML map files when the user expects an inline map; search the specialist catalog and delegate to `map.render` because it returns COSMIC inline map artifacts. "
            "For shell commands, project edits, deployment, screenshots, package-heavy setup, or long-running execution, use COSMIC's specialist routes: "
            "search the agent catalog and delegate to Alpha (`alpha.execute`) for project/VM work, or the relevant tabular/docs specialist for scoped data work. "
            "When current web information is needed, use COSMIC's research routes such as Perplexity, Firecrawl, X search, docs tools, and memory instead of claiming native web access. "
            "Do not say a capability is unavailable until you have considered the available COSMIC specialist/local tools."
        )
        base = str(system_prompt or "").rstrip()
        return f"{base}\n\n{note}" if base else note

    def _messages_to_openai_chat(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "").strip()
            if role not in {"user", "assistant"}:
                continue
            if role == "user":
                content = self._message_content_to_openai_user(message.get("content"))
            else:
                content = self._message_content_to_plain_text(message.get("content"))
            if isinstance(content, list):
                if not content:
                    continue
            elif not str(content or "").strip():
                continue
            if converted and converted[-1].get("role") == role:
                converted[-1]["content"] = self._merge_openai_message_content(
                    converted[-1].get("content"),
                    content,
                    role=role,
                )
                continue
            converted.append({"role": role, "content": content})
        while converted and converted[0].get("role") != "user":
            converted.pop(0)
        return converted

    def _message_content_to_openai_user(self, value: Any) -> str | list[dict[str, Any]]:
        if not isinstance(value, list):
            return str(value or "").strip()
        parts: list[dict[str, Any]] = []
        for block in value:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "").strip()
            if block_type == "text":
                text = str(block.get("text") or "").strip()
                if text:
                    parts.append({"type": "text", "text": text})
                continue
            if block_type == "image":
                source = block.get("source") if isinstance(block.get("source"), dict) else {}
                source_type = str(source.get("type") or "").strip()
                image_url = ""
                if source_type == "url":
                    image_url = str(source.get("url") or "").strip()
                elif source_type == "base64":
                    media_type = str(source.get("media_type") or "image/png").strip() or "image/png"
                    data = str(source.get("data") or "").strip()
                    if data:
                        image_url = f"data:{media_type};base64,{data}"
                if image_url:
                    parts.append({"type": "image_url", "image_url": {"url": image_url}})
                continue
            text = self._message_content_to_plain_text([block]).strip()
            if text:
                parts.append({"type": "text", "text": text})
        if not parts:
            return ""
        if all(part.get("type") == "text" for part in parts):
            return "\n\n".join(str(part.get("text") or "").strip() for part in parts if str(part.get("text") or "").strip())
        return parts

    def _message_content_to_plain_text(self, value: Any) -> str:
        if not isinstance(value, list):
            return str(value or "").strip()
        pieces: list[str] = []
        for block in value:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "").strip()
            if block_type == "text":
                text = str(block.get("text") or "").strip()
                if text:
                    pieces.append(text)
            elif block_type == "tool_result":
                content = str(block.get("content") or "").strip()
                if content:
                    pieces.append(content)
            elif block_type == "image":
                pieces.append("[image attachment omitted from assistant replay]")
            elif block_type in {"tool_use", "server_tool_use"}:
                name = str(block.get("name") or "tool").strip()
                pieces.append(f"[{name} tool call omitted from provider replay]")
            elif block_type.endswith("_tool_result"):
                pieces.append(f"[{block_type} omitted from provider replay]")
        return "\n\n".join(piece for piece in pieces if piece)

    def _merge_openai_message_content(
        self,
        existing: Any,
        incoming: Any,
        *,
        role: str,
    ) -> str | list[dict[str, Any]]:
        if role == "assistant":
            existing_text = self._message_content_to_plain_text(existing)
            incoming_text = self._message_content_to_plain_text(incoming)
            return "\n\n".join(part for part in (existing_text, incoming_text) if part)
        if isinstance(existing, list) or isinstance(incoming, list):
            existing_parts = existing if isinstance(existing, list) else [{"type": "text", "text": str(existing or "").strip()}]
            incoming_parts = incoming if isinstance(incoming, list) else [{"type": "text", "text": str(incoming or "").strip()}]
            return [
                part
                for part in [*existing_parts, *incoming_parts]
                if isinstance(part, dict)
                and (
                    str(part.get("text") or "").strip()
                    or isinstance(part.get("image_url"), dict)
                )
            ]
        existing_text = str(existing or "").strip()
        incoming_text = str(incoming or "").strip()
        return "\n\n".join(part for part in (existing_text, incoming_text) if part)

    @staticmethod
    def _tools_to_openai_chat(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            name = str(tool.get("name") or "").strip()
            parameters = tool.get("input_schema")
            if not name or not isinstance(parameters, dict):
                continue
            converted.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": str(tool.get("description") or "").strip(),
                        "parameters": parameters,
                    },
                }
            )
        return converted

    async def _stream_openai_chat_events(
        self,
        *,
        model_name: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        usage_context: dict[str, Any] | None,
    ) -> AsyncIterator[dict[str, Any]]:
        base_url = self.config.fireworks_base_url.rstrip("/")
        body: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "stream": True,
            "temperature": self.config.fireworks_kimi_temperature,
            "stream_options": {"include_usage": True},
        }
        if self.config.fireworks_kimi_max_tokens is not None:
            body["max_tokens"] = self.config.fireworks_kimi_max_tokens
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        headers = {
            "Authorization": f"Bearer {self.config.fireworks_api_key}",
            "Content-Type": "application/json",
        }
        for attempt in range(3):
            yielded_any = False
            usage: dict[str, Any] = {}
            metered_call = begin_metered_call(prefix="call")
            provider_request_id: str | None = None
            try:
                async with self._client.stream(
                    "POST",
                    f"{base_url}/chat/completions",
                    headers=headers,
                    json=body,
                ) as resp:
                    provider_request_id = (
                        resp.headers.get("x-request-id")
                        or resp.headers.get("request-id")
                        or resp.headers.get("x-fireworks-request-id")
                        or None
                    )
                    if resp.status_code >= 400:
                        raw = await resp.aread()
                        raise RuntimeError(self._error_from_response(raw, resp.status_code))
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if not data_str:
                            continue
                        if data_str == "[DONE]":
                            break
                        parsed = json.loads(data_str)
                        if not isinstance(parsed, dict):
                            continue
                        usage = self._merge_stream_usage(usage, self._extract_openai_usage(parsed))
                        choices = parsed.get("choices")
                        if isinstance(choices, list) and choices:
                            yielded_any = True
                        yield parsed
                await self._record_internal_usage_event(
                    metered_call=metered_call,
                    model_key=build_model_key("fireworks", model_name),
                    usage_context=usage_context,
                    provider_request_id=provider_request_id,
                    raw_usage=usage,
                    success=True,
                    error_code=None,
                    metadata_json={
                        "attempt": attempt + 1,
                        "streaming": True,
                        "yielded_any": yielded_any,
                    },
                )
                return
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                await self._record_internal_usage_event(
                    metered_call=metered_call,
                    model_key=build_model_key("fireworks", model_name),
                    usage_context=usage_context,
                    provider_request_id=provider_request_id,
                    raw_usage=usage,
                    success=False,
                    error_code=type(exc).__name__,
                    metadata_json={
                        "attempt": attempt + 1,
                        "streaming": True,
                        "yielded_any": yielded_any,
                    },
                )
                if yielded_any or attempt == 2:
                    raise RuntimeError(f"Fireworks API error: {exc}") from exc
                await asyncio.sleep(0.5 * (2**attempt))

    async def _finalize_openai_text_without_tools(
        self,
        *,
        model_name: str,
        messages: list[dict[str, Any]],
        usage_context: dict[str, Any] | None,
    ) -> tuple[str, dict[str, int]]:
        """One extra non-tool completion to force a text reply after a turn's
        tool-call budget is exhausted. Without this, a turn that spends every
        iteration on tool calls (for example repeatedly retrying a search
        that keeps failing) exits with an empty response: the loop just
        breaks and ships whatever visible text happened to accumulate, which
        is nothing, since every iteration was a tool call. Passing tools=[]
        omits the tools field from the request body entirely, so the model
        cannot call anything and must answer in words."""
        text_parts: list[str] = []
        usage: dict[str, int] = {}
        async for payload in self._stream_openai_chat_events(
            model_name=model_name,
            messages=messages,
            tools=[],
            usage_context=usage_context,
        ):
            usage_batch = self._extract_openai_usage(payload)
            if usage_batch:
                usage = self._merge_stream_usage(usage, usage_batch)
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            first = choices[0]
            if not isinstance(first, dict):
                continue
            delta = first.get("delta")
            if not isinstance(delta, dict):
                continue
            content_chunk = str(delta.get("content") or "")
            if content_chunk:
                text_parts.append(content_chunk)
        return "".join(text_parts).strip(), usage

    @staticmethod
    def _extract_openai_usage(payload: dict[str, Any]) -> dict[str, int]:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return {}
        result: dict[str, int] = {}
        for key, value in usage.items():
            if isinstance(value, (int, float)):
                result[str(key)] = int(value)
        cached_tokens = OrchestratorRuntime._read_openai_usage_detail(
            usage,
            "prompt_tokens_details",
            "cached_tokens",
        )
        if cached_tokens is not None:
            result["cached_tokens"] = result.get("cached_tokens", 0) + cached_tokens
        reasoning_tokens = OrchestratorRuntime._read_openai_usage_detail(
            usage,
            "completion_tokens_details",
            "reasoning_tokens",
        )
        if reasoning_tokens is not None:
            result["reasoning_tokens"] = result.get("reasoning_tokens", 0) + reasoning_tokens
        return result

    @staticmethod
    def _read_openai_usage_detail(
        usage: dict[str, Any],
        group_key: str,
        detail_key: str,
    ) -> int | None:
        details = usage.get(group_key)
        if not isinstance(details, dict):
            return None
        value = details.get(detail_key)
        if not isinstance(value, (int, float)):
            return None
        return max(0, int(value))

    @staticmethod
    def _extract_openai_tool_call_deltas(delta: dict[str, Any]) -> list[dict[str, Any]]:
        raw = delta.get("tool_calls")
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        raw_function = delta.get("function_call")
        if isinstance(raw_function, dict):
            return [{"index": 0, "function": raw_function}]
        return []

    @staticmethod
    def _normalize_openai_tool_calls(
        tool_calls: dict[int, dict[str, Any]]
    ) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        for index in sorted(tool_calls):
            item = tool_calls[index]
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            tool_id = str(item.get("id") or "").strip() or f"call_{uuid4().hex[:16]}"
            normalized.append(
                {
                    "id": tool_id,
                    "name": name,
                    "arguments": str(item.get("arguments") or ""),
                }
            )
        return normalized

    async def _build_openai_tool_result_followup_content(
        self,
        result_strs: list[str],
    ) -> str | list[dict[str, Any]]:
        artifacts = self._extract_artifacts_from_tool_results(result_strs)
        if not artifacts:
            return ""
        parts: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "Resolved reusable artifact references are available below. "
                    "Use provider_url/download_url/path metadata directly when a specialist or Alpha task needs the file."
                ),
            }
        ]
        seen: set[tuple[str, str]] = set()
        image_blocks = 0
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            artifact_id = str(artifact.get("artifact_id") or "").strip()
            path = str(artifact.get("path") or "").strip()
            dedupe_key = (artifact_id, path)
            if not any(dedupe_key) or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            descriptor = {
                key: artifact.get(key)
                for key in (
                    "artifact_id",
                    "filename",
                    "mime",
                    "kind",
                    "path",
                    "provider_url",
                    "download_url",
                    "sha256",
                )
                if artifact.get(key)
            }
            parts.append(
                {
                    "type": "text",
                    "text": json.dumps(descriptor, ensure_ascii=False, default=str),
                }
            )
            image_url = str(artifact.get("provider_url") or artifact.get("download_url") or "").strip()
            if (
                image_url.startswith(("http://", "https://"))
                and image_blocks < max(1, int(self.config.anthropic_max_input_images))
                and is_supported_image_artifact(artifact)
            ):
                parts.append({"type": "image_url", "image_url": {"url": image_url}})
                image_blocks += 1
        return parts

    def _build_openai_tool_loop_message(
        self,
        tool_calls: list[dict[str, str]],
        parsed_inputs: list[dict[str, Any]],
        result_strs: list[str],
        *,
        parallel: bool,
    ) -> str:
        phrases: list[str] = []
        for item, tool_input, result_str in zip(tool_calls, parsed_inputs, result_strs):
            phrase = self._summarize_local_tool_activity(item["name"], tool_input, result_str)
            if phrase:
                phrases.append(phrase)
        if not phrases:
            tool_names = [item["name"] for item in tool_calls if item.get("name")]
            if not tool_names:
                return "Tool work completed. Continuing..."
            phrases.append(self._format_found_pages_phrase("completed tool work for", tool_names))
        return self._compose_tool_loop_message(phrases, parallel=parallel)

    def _extract_specialist_delegations_from_names(
        self,
        tool_calls: list[dict[str, str]],
        parsed_inputs: list[dict[str, Any]],
        result_strs: list[str],
    ) -> list[dict[str, Any]]:
        delegations: list[dict[str, Any]] = []
        for item, tool_input, result_str in zip(tool_calls, parsed_inputs, result_strs):
            if item.get("name") != "delegate_to_agent":
                continue
            data = self._parse_tool_result_json(result_str)
            delegation = data.get("delegation") if isinstance(data, dict) and isinstance(data.get("delegation"), dict) else {}
            intent_name = self._activity_excerpt(
                delegation.get("intent") or tool_input.get("intent"),
                limit=96,
            )
            agent_id = self._activity_excerpt(
                delegation.get("agent_id") or tool_input.get("agent_id"),
                limit=120,
            )
            task_id = self._activity_excerpt(
                delegation.get("task_id")
                or (data.get("delegated_task_id") if isinstance(data, dict) else None)
                or (data.get("task_id") if isinstance(data, dict) else None),
                limit=96,
            )
            agent_label = self._activity_agent_label(agent_id)
            activity = self._activity_excerpt(
                self._summarize_local_tool_activity(item.get("name") or "", tool_input, result_str),
                limit=160,
            )
            if not (intent_name or agent_id or task_id):
                continue
            delegations.append(
                {
                    key: value
                    for key, value in {
                        "intent": intent_name,
                        "agent_id": agent_id,
                        "agent_label": agent_label,
                        "task_id": task_id,
                        "activity": activity,
                    }.items()
                    if value not in (None, "")
                }
            )
        return delegations

    @staticmethod
    def _estimate_openai_request_context_chars(messages: list[dict[str, Any]]) -> int:
        try:
            return len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")))
        except (TypeError, ValueError):
            return len(repr(messages))

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
        auth_requirement = self._get_auth_requirement_for_intent(recipient, resolved_intent)
        if auth_requirement and not isinstance(child_input.get("auth"), dict):
            child_input["auth"] = await self._resolve_auth_for_child_task(
                parent_task=parent_task,
                recipient=recipient,
                intent=resolved_intent,
                child_input=child_input,
                auth_requirement=auth_requirement,
            )

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
                result = await asyncio.wait_for(
                    asyncio.shield(pending_result), timeout=wait_timeout
                )
                if isinstance(result, AgentResult):
                    output = (
                        dict(result.output)
                        if isinstance(result.output, dict)
                        else {}
                    )
                    output.setdefault("delegated_task_id", child_task.task_id)
                    return result.model_copy(update={"output": output})
                return result
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

    async def accept_reverse_task(self, task: TaskEnvelope) -> dict[str, Any]:
        if task.recipient != self.config.orchestrator_agent_id:
            raise RuntimeError("reverse task recipient must be the orchestrator.")
        if task.source != "agent":
            raise RuntimeError("reverse tasks must use source='agent'.")
        if not verify_task_envelope(task, self._resolve_agent_secret(task.sender)):
            raise RuntimeError("reverse task signature verification failed.")

        if task.intent == "orchestrator.delegate":
            return await self._register_reverse_delegate(task)
        if task.intent == "orchestrator.refresh_credential":
            return await self._register_reverse_refresh(task)
        raise RuntimeError(f"Unsupported reverse intent: {task.intent}")

    async def _register_reverse_delegate(self, task: TaskEnvelope) -> dict[str, Any]:
        waiting_task_id = str(task.parent_task_id or "").strip()
        if not waiting_task_id:
            raise RuntimeError("orchestrator.delegate requires parent_task_id pointing to the waiting specialist task.")
        waiting_record = self.task_ledger.get_task(waiting_task_id)
        if waiting_record is None:
            raise RuntimeError(f"Waiting task {waiting_task_id!r} was not found in the task ledger.")
        waiting_recipient = str(waiting_record.get("recipient") or "").strip()
        if waiting_recipient != task.sender:
            raise RuntimeError("reverse task sender must own the waiting specialist task.")

        target_intent = str(task.input.get("target_intent") or "").strip()
        target_input = task.input.get("target_input")
        if not target_intent:
            raise RuntimeError("orchestrator.delegate requires target_intent.")
        if not isinstance(target_input, dict):
            raise RuntimeError("orchestrator.delegate requires target_input object.")

        target_agent_id = str(task.input.get("target_agent_id") or "").strip() or None
        resume_payload = task.input.get("resume_payload") if isinstance(task.input.get("resume_payload"), dict) else {}

        self.task_ledger.create_task(task)
        self.task_ledger.create_reverse_task_wait(
            reverse_task_id=task.task_id,
            waiting_task_id=waiting_task_id,
            parent_task_id=str(waiting_record.get("parent_task_id") or "").strip() or None,
            sender=task.sender,
            recipient=waiting_recipient,
            reverse_intent=task.intent,
            target_intent=target_intent,
            target_agent_id=target_agent_id,
            reverse_payload={**task.input, "resume_payload": resume_payload},
        )
        self.task_ledger.mark_suspended(
            task.task_id,
            payload={
                "reason": "reverse_delegate_registered",
                "waiting_task_id": waiting_task_id,
                "target_intent": target_intent,
                "target_agent_id": target_agent_id,
            },
        )
        return {
            "reverse_task_id": task.task_id,
            "status": "registered",
            "waiting_task_id": waiting_task_id,
            "target_intent": target_intent,
            "target_agent_id": target_agent_id,
        }

    async def _register_reverse_refresh(self, task: TaskEnvelope) -> dict[str, Any]:
        waiting_task_id = str(task.parent_task_id or "").strip()
        if not waiting_task_id:
            raise RuntimeError("orchestrator.refresh_credential requires parent_task_id pointing to the waiting specialist task.")
        waiting_record = self.task_ledger.get_task(waiting_task_id)
        if waiting_record is None:
            raise RuntimeError(f"Waiting task {waiting_task_id!r} was not found in the task ledger.")
        waiting_recipient = str(waiting_record.get("recipient") or "").strip()
        if waiting_recipient != task.sender:
            raise RuntimeError("reverse task sender must own the waiting specialist task.")

        credential_ref = str(task.input.get("credential_ref") or "").strip()
        provider = str(task.input.get("provider") or "").strip() or None
        if not credential_ref:
            raise RuntimeError("orchestrator.refresh_credential requires credential_ref.")

        self.task_ledger.create_task(task)
        self.task_ledger.create_reverse_task_wait(
            reverse_task_id=task.task_id,
            waiting_task_id=waiting_task_id,
            parent_task_id=str(waiting_record.get("parent_task_id") or "").strip() or None,
            sender=task.sender,
            recipient=waiting_recipient,
            reverse_intent=task.intent,
            target_intent=None,
            target_agent_id=None,
            reverse_payload={**task.input, "provider": provider},
        )
        self.task_ledger.mark_suspended(
            task.task_id,
            payload={
                "reason": "credential_refresh_registered",
                "waiting_task_id": waiting_task_id,
                "credential_ref": credential_ref,
                "provider": provider,
            },
        )
        return {
            "reverse_task_id": task.task_id,
            "status": "registered",
            "waiting_task_id": waiting_task_id,
            "credential_ref": credential_ref,
            "provider": provider,
        }

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

    def _get_auth_requirement_for_intent(self, agent_id: str, intent: str) -> dict[str, Any] | None:
        card = self.registry_store.get_card(agent_id)
        if not isinstance(card, dict):
            return None
        auth_requirements = card.get("auth_requirements")
        if not isinstance(auth_requirements, dict):
            return None
        requirement = auth_requirements.get(intent)
        return dict(requirement) if isinstance(requirement, dict) else None

    async def _resolve_auth_for_child_task(
        self,
        *,
        parent_task: TaskEnvelope,
        recipient: str,
        intent: str,
        child_input: dict[str, Any],
        auth_requirement: dict[str, Any],
    ) -> dict[str, Any]:
        provider = str(auth_requirement.get("provider") or "").strip()
        scopes = [
            str(item).strip()
            for item in (auth_requirement.get("scopes") or [])
            if str(item).strip()
        ]
        if not provider or not scopes:
            raise RuntimeError(f"Invalid auth_requirements for {recipient}::{intent}.")

        payload: dict[str, Any] = {
            "provider": provider,
            "required_scopes": scopes,
            "session_id": parent_task.session_id,
            "operation_mode": self._classify_auth_operation_mode(intent),
            "allow_primary_fallback": self._allow_primary_fallback(intent),
        }

        explicit_account_id = self._extract_explicit_account_id(child_input)
        if explicit_account_id:
            payload["account_id"] = explicit_account_id

        account_hint = self._extract_account_hint(child_input)
        if account_hint:
            payload["account_hint"] = account_hint

        resource_hint = str(child_input.get("resource_hint") or "").strip()
        if resource_hint:
            payload["resource_hint"] = resource_hint

        headers = {"Content-Type": "application/json"}
        if self.config.internal_token:
            headers["X-Internal-Token"] = self.config.internal_token

        url = f"{self.config.gateway_url.rstrip('/')}/internal/credentials/resolve"
        try:
            response = await self._client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Credential resolution failed for {intent}: unable to reach Gateway ({exc})."
            ) from exc

        if response.status_code == 409:
            detail = {}
            try:
                detail = response.json().get("detail", {})
            except Exception:
                detail = {}
            accounts = detail.get("accounts") if isinstance(detail, dict) else None
            options: list[str] = []
            if isinstance(accounts, list):
                for account in accounts:
                    if not isinstance(account, dict):
                        continue
                    email = str(account.get("email") or "").strip()
                    display_label = str(account.get("account_display_label") or "").strip()
                    raw_label = str(account.get("account_label") or "").strip()
                    display_name = str(account.get("display_name") or "").strip()
                    label = email or display_label or raw_label or display_name or str(account.get("account_id") or "account").strip()
                    if email and display_label and display_label != email:
                        label = f"{display_label} <{email}>"
                    if bool(account.get("is_primary")):
                        label = f"{label} (primary)"
                    options.append(label)
            suffix = f" Available accounts: {', '.join(options)}." if options else ""
            raise RuntimeError(
                f"Multiple {provider} accounts are connected for {intent}. Ask the user which account to use or pass account_hint.{suffix}"
            )
        if response.status_code == 404:
            raise RuntimeError(
                f"No matching {provider} credential/account reference is available for {intent}. "
                "Check the account_id/account_hint and ask for the target account if ambiguous; "
                "only ask the user to reconnect when the account is inactive, missing, or missing required scopes."
            )
        if response.status_code == 403:
            raise RuntimeError(
                f"Credential resolution for {intent} was rejected by the Gateway."
            )
        response.raise_for_status()
        resolved = response.json()
        if not isinstance(resolved, dict) or not str(resolved.get("access_token") or "").strip():
            raise RuntimeError(f"Gateway returned an invalid credential payload for {intent}.")
        return resolved

    async def _refresh_credential_via_gateway(self, credential_ref: str) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.config.internal_token:
            headers["X-Internal-Token"] = self.config.internal_token
        url = f"{self.config.gateway_url.rstrip('/')}/internal/credentials/refresh"
        try:
            response = await self._client.post(
                url,
                json={"credential_ref": credential_ref},
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Gateway credential refresh failed: {exc}") from exc
        if response.status_code == 404:
            raise RuntimeError("Credential refresh failed because the credential was not found.")
        if response.status_code == 403:
            raise RuntimeError("Credential refresh was rejected by the Gateway.")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not str(payload.get("access_token") or "").strip():
            raise RuntimeError("Gateway returned an invalid refreshed credential payload.")
        return payload

    def _extract_explicit_account_id(self, child_input: dict[str, Any]) -> str | None:
        direct = str(child_input.get("account_id") or "").strip()
        if self._looks_like_google_account_id(direct):
            return direct

        nested_account = child_input.get("account")
        if isinstance(nested_account, dict):
            for key in ("account_id", "id", "credential_account_id"):
                value = str(nested_account.get(key) or "").strip()
                if self._looks_like_google_account_id(value):
                    return value
        return None

    def _looks_like_google_account_id(self, value: str) -> bool:
        normalized = str(value or "").strip()
        return normalized.startswith("acc_")

    def _extract_account_hint(self, child_input: dict[str, Any]) -> str | None:
        candidate_keys = (
            "account_hint",
            "account_name",
            "account_label",
            "account_email",
            "selected_account",
            "target_account",
            "target_account_email",
            "calendar_account",
            "calendar_account_hint",
            "docs_account",
            "docs_account_hint",
            "drive_account",
            "drive_account_hint",
            "gmail_account",
            "gmail_account_hint",
            "google_account",
            "google_account_hint",
            "google_account_email",
        )
        for key in candidate_keys:
            value = child_input.get(key)
            if value is None:
                continue
            normalized = self._normalize_account_hint_value(value)
            if normalized:
                return normalized

        direct_account_id = str(child_input.get("account_id") or "").strip()
        if direct_account_id and not self._looks_like_google_account_id(direct_account_id):
            return direct_account_id

        nested_account = child_input.get("account")
        if isinstance(nested_account, dict):
            for key in (
                "account_hint",
                "account_email",
                "email",
                "account_label",
                "account_display_label",
                "display_name",
                "name",
            ):
                normalized = self._normalize_account_hint_value(nested_account.get(key))
                if normalized:
                    return normalized
        elif nested_account is not None:
            normalized = self._normalize_account_hint_value(nested_account)
            if normalized:
                return normalized
        return None

    def _normalize_account_hint_value(self, value: Any) -> str | None:
        if value is None or isinstance(value, (dict, list, tuple, set)):
            return None
        normalized = str(value).strip()
        return normalized or None

    def _classify_auth_operation_mode(self, intent: str) -> str:
        lowered = str(intent or "").strip().lower()
        write_markers = ("create", "update", "cancel", "delete", "send", "write", "patch", "set")
        return "write" if any(marker in lowered for marker in write_markers) else "read"

    def _allow_primary_fallback(self, intent: str) -> bool:
        return self._classify_auth_operation_mode(intent) == "read"

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

    async def _process_pending_reverse_waits_for_waiting_task(self, waiting_task_id: str) -> None:
        waits = self.task_ledger.list_pending_reverse_task_waits(waiting_task_id)
        for wait in waits:
            await self._dispatch_registered_reverse_wait(wait)

    async def _dispatch_registered_reverse_wait(self, wait: dict[str, Any]) -> None:
        reverse_intent = str(wait.get("reverse_intent") or "").strip()
        if reverse_intent == "orchestrator.refresh_credential":
            await self._process_registered_refresh_wait(wait)
            return

        reverse_task_id = str(wait.get("reverse_task_id") or "").strip()
        waiting_task_id = str(wait.get("waiting_task_id") or "").strip()
        reverse_record = self.task_ledger.get_task(reverse_task_id)
        waiting_record = self.task_ledger.get_task(waiting_task_id)
        if reverse_record is None or waiting_record is None:
            self.task_ledger.mark_reverse_task_wait_failed(
                reverse_task_id,
                code="REVERSE_WAIT_INVALID",
                message="Missing reverse task or waiting task in the ledger.",
            )
            if reverse_record is not None:
                self.task_ledger.mark_failed(
                    reverse_task_id,
                    code="REVERSE_WAIT_INVALID",
                    message="Missing reverse task or waiting task in the ledger.",
                )
            return

        reverse_envelope_json = reverse_record.get("envelope_json")
        if not isinstance(reverse_envelope_json, dict):
            self.task_ledger.mark_reverse_task_wait_failed(
                reverse_task_id,
                code="REVERSE_WAIT_INVALID",
                message="Reverse task ledger record is missing envelope_json.",
            )
            self.task_ledger.mark_failed(
                reverse_task_id,
                code="REVERSE_WAIT_INVALID",
                message="Reverse task ledger record is missing envelope_json.",
            )
            return

        reverse_task = TaskEnvelope.model_validate(reverse_envelope_json)
        target_intent = str(wait.get("target_intent") or "").strip()
        reverse_payload = wait.get("reverse_payload_json") if isinstance(wait.get("reverse_payload_json"), dict) else {}
        target_input = reverse_payload.get("target_input")
        if not target_intent or not isinstance(target_input, dict):
            failure = AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="INVALID_REVERSE_TASK",
                    retryable=False,
                    message="Reverse delegate task is missing target_intent or target_input.",
                    next_action="escalate",
                ),
            )
            await self._dispatch_resumed_task_for_reverse_result(wait, failure, delegated_task_id=None)
            return

        try:
            delegated = await self.dispatch_agent_task(
                parent_task=reverse_task,
                intent=target_intent,
                input_payload=target_input,
                input_artifacts=reverse_task.input_artifacts,
                agent_id=str(wait.get("target_agent_id") or "").strip() or None,
                wait_timeout_sec=0.0,
            )
        except Exception as exc:
            failure = AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="REVERSE_DISPATCH_ERROR",
                    retryable=True,
                    message=str(exc).strip()[:500] or "Reverse delegate dispatch failed.",
                    next_action="retry",
                ),
            )
            await self._dispatch_resumed_task_for_reverse_result(wait, failure, delegated_task_id=None)
            return

        if isinstance(delegated, TaskInProgress):
            self.task_ledger.mark_reverse_task_wait_dispatched(
                reverse_task_id,
                delegated_task_id=delegated.task_id,
            )
            self.task_ledger.mark_deferred(
                reverse_task_id,
                result=delegated.model_dump(mode="json"),
            )
            return

        await self._dispatch_resumed_task_for_reverse_result(wait, delegated, delegated_task_id=None)

    async def _process_registered_refresh_wait(self, wait: dict[str, Any]) -> None:
        reverse_task_id = str(wait.get("reverse_task_id") or "").strip()
        waiting_task_id = str(wait.get("waiting_task_id") or "").strip()
        waiting_record = self.task_ledger.get_task(waiting_task_id)
        if waiting_record is None:
            self.task_ledger.mark_reverse_task_wait_failed(
                reverse_task_id,
                code="WAITING_TASK_MISSING",
                message="Waiting specialist task no longer exists.",
            )
            self.task_ledger.mark_failed(
                reverse_task_id,
                code="WAITING_TASK_MISSING",
                message="Waiting specialist task no longer exists.",
            )
            return

        reverse_payload = wait.get("reverse_payload_json") if isinstance(wait.get("reverse_payload_json"), dict) else {}
        credential_ref = str(reverse_payload.get("credential_ref") or "").strip()
        if not credential_ref:
            error_message = "Credential refresh request is missing credential_ref."
            self.task_ledger.mark_reverse_task_wait_failed(
                reverse_task_id,
                code="INVALID_REVERSE_TASK",
                message=error_message,
            )
            self.task_ledger.mark_failed(
                reverse_task_id,
                code="INVALID_REVERSE_TASK",
                message=error_message,
            )
            self.task_ledger.mark_failed(
                waiting_task_id,
                code="AUTH_ERROR",
                message=error_message,
            )
            self._resolve_pending_agent_result(
                waiting_task_id,
                AgentResult(
                    status="failed",
                    output={},
                    artifacts=[],
                    error=AgentError(
                        code="AUTH_ERROR",
                        retryable=False,
                        message=error_message,
                        next_action="escalate",
                    ),
                ),
            )
            return

        try:
            refreshed_auth = await self._refresh_credential_via_gateway(credential_ref)
        except Exception as exc:
            message = str(exc).strip() or "Credential refresh failed."
            self.task_ledger.mark_reverse_task_wait_failed(
                reverse_task_id,
                code="AUTH_ERROR",
                message=message,
            )
            self.task_ledger.mark_failed(
                reverse_task_id,
                code="AUTH_ERROR",
                message=message,
            )
            self.task_ledger.mark_failed(
                waiting_task_id,
                code="AUTH_ERROR",
                message=message,
            )
            self._resolve_pending_agent_result(
                waiting_task_id,
                AgentResult(
                    status="failed",
                    output={},
                    artifacts=[],
                    error=AgentError(
                        code="AUTH_ERROR",
                        retryable=False,
                        message=message,
                        next_action="escalate",
                    ),
                ),
            )
            return

        await self._dispatch_resumed_task_for_refreshed_credential(wait, refreshed_auth)

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

    async def _dispatch_resumed_task_for_reverse_result(
        self,
        wait: dict[str, Any],
        result: AgentResult,
        *,
        delegated_task_id: str | None,
    ) -> None:
        if self._redis is None:
            return

        reverse_task_id = str(wait.get("reverse_task_id") or "").strip()
        waiting_task_id = str(wait.get("waiting_task_id") or "").strip()
        waiting_record = self.task_ledger.get_task(waiting_task_id)
        if waiting_record is None:
            self.task_ledger.mark_reverse_task_wait_failed(
                reverse_task_id,
                code="WAITING_TASK_MISSING",
                message="Waiting specialist task no longer exists.",
            )
            self.task_ledger.mark_failed(
                reverse_task_id,
                code="WAITING_TASK_MISSING",
                message="Waiting specialist task no longer exists.",
            )
            return

        waiting_envelope = waiting_record.get("envelope_json") if isinstance(waiting_record.get("envelope_json"), dict) else {}
        reverse_payload = wait.get("reverse_payload_json") if isinstance(wait.get("reverse_payload_json"), dict) else {}
        reverse_task_meta = {
            "reverse_task_id": reverse_task_id,
            "intent": str(wait.get("reverse_intent") or "").strip() or "orchestrator.delegate",
            "target_intent": str(wait.get("target_intent") or "").strip() or None,
            "target_agent_id": str(wait.get("target_agent_id") or "").strip() or None,
            "delegated_task_id": delegated_task_id,
        }
        resume_input = {
            "resume_of_task_id": waiting_task_id,
            "resume_intent": str(waiting_record.get("intent") or "").strip(),
            "resume_input": dict(waiting_envelope.get("input") or {}),
            "resume_state": dict(reverse_payload.get("resume_payload") or {}),
            "reply": {},
            "reverse_task": reverse_task_meta,
            "reverse_result": result.model_dump(mode="json"),
        }
        resume_task = TaskEnvelope(
            task_id=generate_task_id(),
            task_list_id=str(waiting_record.get("task_list_id") or "").strip(),
            parent_task_id=waiting_task_id,
            session_id=str(waiting_record.get("session_id") or "").strip() or None,
            sender=self.config.orchestrator_agent_id,
            recipient=str(wait.get("recipient") or waiting_record.get("recipient") or "").strip(),
            intent=str(reverse_payload.get("resume_intent") or "agent.resume").strip() or "agent.resume",
            input=resume_input,
            input_artifacts=list(waiting_envelope.get("input_artifacts") or []),
            idempotency_key=f"{str(waiting_record.get('idempotency_key') or '').strip()}:resume:{reverse_task_id}",
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
        self.task_ledger.mark_reverse_task_wait_resumed(
            reverse_task_id,
            resumed_task_id=resume_task.task_id,
        )
        if result.status == "completed":
            self.task_ledger.mark_completed(
                reverse_task_id,
                result={
                    "status": "completed",
                    "delegated_task_id": delegated_task_id,
                    "target_intent": reverse_task_meta.get("target_intent"),
                    "target_agent_id": reverse_task_meta.get("target_agent_id"),
                    "resume_task_id": resume_task.task_id,
                },
            )
        else:
            error = result.error or AgentError(
                code="REVERSE_DELEGATE_FAILED",
                retryable=False,
                message="Delegated specialist failed.",
                next_action="escalate",
            )
            self.task_ledger.mark_failed(
                reverse_task_id,
                code=error.code,
                message=error.message,
            )
        self.task_ledger.mark_resumed(
            waiting_task_id,
            payload={
                "reverse_task_id": reverse_task_id,
                "delegated_task_id": delegated_task_id,
                "resume_task_id": resume_task.task_id,
                "target_intent": reverse_task_meta.get("target_intent"),
                "target_agent_id": reverse_task_meta.get("target_agent_id"),
                "result_status": result.status,
            },
        )
        self._link_pending_agent_result_alias(alias_task_id=resume_task.task_id, canonical_task_id=waiting_task_id)
        await dispatch_task(resume_task, self._redis)

    async def _dispatch_resumed_task_for_refreshed_credential(
        self,
        wait: dict[str, Any],
        refreshed_auth: dict[str, Any],
    ) -> None:
        if self._redis is None:
            return

        reverse_task_id = str(wait.get("reverse_task_id") or "").strip()
        waiting_task_id = str(wait.get("waiting_task_id") or "").strip()
        waiting_record = self.task_ledger.get_task(waiting_task_id)
        if waiting_record is None:
            self.task_ledger.mark_reverse_task_wait_failed(
                reverse_task_id,
                code="WAITING_TASK_MISSING",
                message="Waiting specialist task no longer exists.",
            )
            self.task_ledger.mark_failed(
                reverse_task_id,
                code="WAITING_TASK_MISSING",
                message="Waiting specialist task no longer exists.",
            )
            return

        waiting_envelope = waiting_record.get("envelope_json") if isinstance(waiting_record.get("envelope_json"), dict) else {}
        reverse_payload = wait.get("reverse_payload_json") if isinstance(wait.get("reverse_payload_json"), dict) else {}
        reverse_task_meta = {
            "reverse_task_id": reverse_task_id,
            "intent": "orchestrator.refresh_credential",
            "credential_ref": str(refreshed_auth.get("credential_ref") or "").strip() or None,
            "provider": str(refreshed_auth.get("provider") or reverse_payload.get("provider") or "").strip() or None,
            "delegated_task_id": None,
        }
        resume_input = {
            "auth": dict(refreshed_auth),
            "resume_of_task_id": waiting_task_id,
            "resume_intent": str(waiting_record.get("intent") or "").strip(),
            "resume_input": dict(waiting_envelope.get("input") or {}),
            "resume_state": {},
            "reply": {},
            "reverse_task": reverse_task_meta,
            "reverse_result": {
                "status": "completed",
                "output": {
                    "credential_ref": reverse_task_meta["credential_ref"],
                    "provider": reverse_task_meta["provider"],
                    "refreshed": True,
                },
                "artifacts": [],
            },
        }
        resume_task = TaskEnvelope(
            task_id=generate_task_id(),
            task_list_id=str(waiting_record.get("task_list_id") or "").strip(),
            parent_task_id=waiting_task_id,
            session_id=str(waiting_record.get("session_id") or "").strip() or None,
            sender=self.config.orchestrator_agent_id,
            recipient=str(wait.get("recipient") or waiting_record.get("recipient") or "").strip(),
            intent="agent.resume",
            input=resume_input,
            input_artifacts=list(waiting_envelope.get("input_artifacts") or []),
            idempotency_key=f"{str(waiting_record.get('idempotency_key') or '').strip()}:resume:{reverse_task_id}",
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
        self.task_ledger.mark_reverse_task_wait_resumed(
            reverse_task_id,
            resumed_task_id=resume_task.task_id,
        )
        self.task_ledger.mark_completed(
            reverse_task_id,
            result={
                "status": "completed",
                "credential_ref": reverse_task_meta["credential_ref"],
                "provider": reverse_task_meta["provider"],
                "resume_task_id": resume_task.task_id,
            },
        )
        self.task_ledger.mark_resumed(
            waiting_task_id,
            payload={
                "reverse_task_id": reverse_task_id,
                "resume_task_id": resume_task.task_id,
                "refresh_credential_ref": reverse_task_meta["credential_ref"],
                "provider": reverse_task_meta["provider"],
            },
        )
        self._link_pending_agent_result_alias(alias_task_id=resume_task.task_id, canonical_task_id=waiting_task_id)
        await dispatch_task(resume_task, self._redis)

    def _fail_open_reverse_waits_for_waiting_task(
        self,
        waiting_task_id: str,
        *,
        code: str,
        message: str,
    ) -> None:
        for wait in self.task_ledger.list_open_reverse_task_waits(waiting_task_id):
            reverse_task_id = str(wait.get("reverse_task_id") or "").strip()
            self.task_ledger.mark_reverse_task_wait_failed(
                reverse_task_id,
                code=code,
                message=message,
            )
            reverse_task_record = self.task_ledger.get_task(reverse_task_id)
            if reverse_task_record is not None and str(reverse_task_record.get("status") or "").strip() not in {"completed", "failed"}:
                self.task_ledger.mark_failed(reverse_task_id, code=code, message=message)

    async def _agent_event_consumer_loop(self) -> None:
        assert self._redis is not None
        backoff_sec = 1.0
        while True:
            try:
                entries = await self._redis.xreadgroup(
                    groupname=self.config.agent_events_group,
                    consumername=self._agent_event_consumer_name,
                    streams={self.config.agent_events_stream: ">"},
                    count=20,
                    block=1000,
                )
                backoff_sec = 1.0
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
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "orchestrator.agent_event_consumer_loop_failed retry_in=%.1fs",
                    backoff_sec,
                )
                await asyncio.sleep(backoff_sec)
                backoff_sec = min(backoff_sec * 2, 30.0)

    async def _handle_agent_event(self, event: EventEnvelope) -> None:
        self._agent_dispatch_stats.events_consumed += 1
        resume_wait = self.task_ledger.get_task_input_wait_by_resumed_task(event.task_id)
        if resume_wait is None:
            resume_wait = self.task_ledger.get_reverse_task_wait_by_resumed_task(event.task_id)
        canonical_task_id = str(resume_wait.get("waiting_task_id") or "").strip() if resume_wait else event.task_id
        delegated_wait = self.task_ledger.get_reverse_task_wait_by_delegated_task(event.task_id)
        canonical_record = self.task_ledger.get_task(canonical_task_id)
        event_record = self.task_ledger.get_task(event.task_id)
        if event.event_type in {"task.completed", "task.failed", "task.cancelled", "task.dlq"} and (
            str((canonical_record or {}).get("status") or "").strip() == "cancelled"
            or str((event_record or {}).get("status") or "").strip() == "cancelled"
        ):
            self._agent_dispatch_stats.events_consumed += 0
            return

        if event.event_type == "task.cancelled":
            message = str(event.payload.get("message") or "Task cancelled.").strip()
            self.task_ledger.mark_cancelled(event.task_id, message=message)
            if canonical_task_id != event.task_id:
                self.task_ledger.mark_cancelled(canonical_task_id, message=message)
            result = AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="CANCELLED",
                    retryable=False,
                    message=message,
                    next_action="skip",
                ),
            )
            self._resolve_pending_agent_result(canonical_task_id, result)
            return

        if event.event_type == "task.completed":
            task_record = self.task_ledger.get_task(canonical_task_id)
            result = self._coerce_agent_result(event)
            self.task_ledger.mark_completed(event.task_id, result=result.model_dump(mode="json"))
            if canonical_task_id != event.task_id:
                self.task_ledger.mark_completed(canonical_task_id, result=result.model_dump(mode="json"))
            if delegated_wait is not None:
                self._agent_dispatch_stats.dispatches_completed += 1
                await self._dispatch_resumed_task_for_reverse_result(
                    delegated_wait,
                    result,
                    delegated_task_id=event.task_id,
                )
                return
            self._fail_open_reverse_waits_for_waiting_task(
                canonical_task_id,
                code="WAITING_TASK_COMPLETED",
                message="Waiting specialist task completed before its reverse delegation finished.",
            )
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
            if delegated_wait is not None:
                self._agent_dispatch_stats.failed_events += 1
                await self._dispatch_resumed_task_for_reverse_result(
                    delegated_wait,
                    result,
                    delegated_task_id=event.task_id,
                )
                return
            self._fail_open_reverse_waits_for_waiting_task(
                canonical_task_id,
                code=error.code,
                message=error.message,
            )
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
            await self._process_pending_reverse_waits_for_waiting_task(canonical_task_id)
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
            if delegated_wait is not None:
                self._agent_dispatch_stats.rejected_events += 1
                await self._dispatch_resumed_task_for_reverse_result(
                    delegated_wait,
                    result,
                    delegated_task_id=event.task_id,
                )
                return
            self._fail_open_reverse_waits_for_waiting_task(
                canonical_task_id,
                code=error.code,
                message=error.message,
            )
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
        model_override: str | None = None,
    ) -> AsyncIterator[SSEEvent]:
        url = "https://api.anthropic.com/v1/messages"
        model_name = str(model_override or self.config.anthropic_model).strip() or self.config.anthropic_model
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
            "model": model_name,
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

        headers = self._anthropic_api_headers(accept="text/event-stream", include_code_execution_beta=True)
        headers["content-type"] = "application/json"

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
                                        usage = self._merge_stream_usage(usage, message.get("usage"))
                                elif ptype == "message_delta":
                                    usage = self._merge_stream_usage(usage, payload.get("usage"))
                        yield item
                await self._record_internal_usage_event(
                    metered_call=metered_call,
                    model_key=build_model_key("anthropic", model_name),
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
                    model_key=build_model_key("anthropic", model_name),
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

    async def _attach_initial_input_artifact_blocks(
        self,
        messages: list[dict[str, Any]],
        input_artifacts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not messages or not input_artifacts:
            return messages
        last_message = messages[-1]
        if not isinstance(last_message, dict) or last_message.get("role") != "user":
            return messages
        staged_blocks = await self._build_artifact_followup_blocks(
            input_artifacts,
            include_visual_image_blocks=False,
        )
        if not staged_blocks:
            return messages
        last_message["content"] = self._merge_message_content(last_message.get("content"), staged_blocks)
        return messages

    async def _build_tool_result_followup_blocks(self, result_strs: list[str]) -> list[dict[str, Any]]:
        artifacts = self._extract_artifacts_from_tool_results(result_strs)
        if not artifacts:
            return []
        staged_blocks = await self._build_artifact_followup_blocks(
            artifacts,
            include_visual_image_blocks=True,
        )
        if not staged_blocks:
            return []
        note = {
            "type": "text",
            "text": (
                "Resolved reusable artifacts are attached below. Any container_upload files are available "
                "to the code_execution tool if you choose it."
            ),
        }
        return [note, *staged_blocks]

    def _extract_artifacts_from_tool_results(self, result_strs: list[str]) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for result_str in result_strs:
            payload = self._parse_tool_result_json(result_str)
            if not isinstance(payload, dict):
                continue
            raw_artifacts = payload.get("artifacts")
            if not isinstance(raw_artifacts, list):
                raw_artifacts = payload.get("results")
            if not isinstance(raw_artifacts, list):
                continue
            for item in raw_artifacts:
                if not isinstance(item, dict):
                    continue
                artifact_id = str(item.get("artifact_id") or "").strip()
                path = str(item.get("path") or "").strip()
                dedupe_key = (artifact_id, path)
                if not any(dedupe_key) or dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                merged.append(dict(item))
        return merged

    async def _build_artifact_followup_blocks(
        self,
        artifacts: list[dict[str, Any]],
        *,
        include_visual_image_blocks: bool,
    ) -> list[dict[str, Any]]:
        if not artifacts:
            return []
        blocks: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        image_blocks = 0
        staged_uploads = 0
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            artifact_id = str(artifact.get("artifact_id") or "").strip()
            path = str(artifact.get("path") or "").strip()
            dedupe_key = (artifact_id, path)
            if not any(dedupe_key) or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            routing_note = self._describe_routable_artifact(artifact)
            if routing_note is not None:
                blocks.append(routing_note)
                if str(artifact.get("kind") or "").strip().lower() == "bundle":
                    # An archive's bytes are never worth a context slot. The
                    # manifest above says what is in it; the files themselves
                    # belong in a workspace, not in the conversation.
                    continue
            payload = await self._load_artifact_payload(artifact)
            if payload is None:
                continue
            if include_visual_image_blocks and is_supported_image_artifact(artifact):
                if image_blocks < max(1, int(self.config.anthropic_max_input_images)):
                    blocks.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": payload.mime_type,
                                "data": base64.b64encode(payload.content).decode("ascii"),
                            },
                        }
                    )
                    image_blocks += 1
            if staged_uploads >= max(0, int(self.config.anthropic_max_staged_input_files)):
                continue
            file_id = await self._upload_input_artifact_to_anthropic(payload)
            if not file_id:
                continue
            blocks.append({"type": "container_upload", "file_id": file_id})
            staged_uploads += 1
        return blocks

    @staticmethod
    def _artifact_archive_manifest(artifact: dict[str, Any]) -> dict[str, Any]:
        """Find the archive manifest wherever the ingest path left it.

        The gateway attaches it under `metadata.archive`, but artifacts that pass
        through persist_inbound_attachments get their whole dict swept into a
        passthrough `metadata` bag -- and that bag does not exclude the key
        `metadata`, so a round trip nests it one level deeper. Rather than depend
        on which branch a given upload took, look through a couple of levels.
        """
        node: Any = artifact.get("metadata")
        for _ in range(3):
            if not isinstance(node, dict):
                return {}
            archive = node.get("archive")
            if isinstance(archive, dict):
                return archive
            node = node.get("metadata")
        return {}

    def _describe_routable_artifact(self, artifact: dict[str, Any]) -> dict[str, Any] | None:
        """Describe an upload that has somewhere to go other than this conversation.

        Returns a text block stating what the file is and which handlers can take
        it -- options, not a decision. Choosing is the orchestrator's job: it is
        the only place that can see the file, the conversation, and what the user
        actually asked for at the same time.
        """
        claims = describe_claims(artifact)
        if not claims:
            return None
        kind = str(artifact.get("kind") or "").strip().lower()
        filename = str(artifact.get("filename") or "").strip() or "attachment"

        lines = [f"Uploaded file: {filename} (kind: {kind})"]

        archive = self._artifact_archive_manifest(artifact)
        if archive:
            summary_bits = [
                f"{archive.get('file_count', 0)} files",
                f"looks like: {archive.get('project_kind', 'unknown')}",
            ]
            if archive.get("common_root"):
                summary_bits.append(f"root directory: {archive['common_root']}/")
            if archive.get("signals"):
                summary_bits.append("markers: " + ", ".join(str(x) for x in archive["signals"][:6]))
            lines.append("Archive contents: " + "; ".join(summary_bits) + ".")
            top_level = archive.get("top_level")
            if isinstance(top_level, list) and top_level:
                lines.append("Top level: " + ", ".join(str(x) for x in top_level[:20]))
            lines.append(
                "The archive itself has not been read into this conversation. "
                "Staging it with a specialist unpacks it into that agent's workspace."
            )

        lines.append("Ways this file can be used:")
        for option in claims.get("options", []):
            intent = option.get("intent")
            target = f" (via {intent})" if intent else ""
            lines.append(f"  - {option['label']}{target}: {option['summary']}")
        if claims.get("ambiguous"):
            lines.append(
                "More than one of these applies. If the user's message does not make the "
                "intent clear, ask which they want before acting."
            )
        return {"type": "text", "text": "\n".join(lines)}

    async def _load_artifact_payload(self, artifact: dict[str, Any]) -> InputArtifactPayload | None:
        filename = self._sanitize_generated_filename(
            str(artifact.get("filename") or "").strip(),
            fallback=Path(str(artifact.get("path") or "")).name or "artifact.bin",
        )
        mime_type = (
            str(artifact.get("mime") or artifact.get("mime_type") or "").strip()
            or mimetypes.guess_type(filename)[0]
            or "application/octet-stream"
        )
        resolved_path = self._resolve_logical_artifact_input_path(str(artifact.get("path") or "").strip())
        content: bytes | None = None
        if resolved_path and resolved_path.is_file():
            try:
                content = resolved_path.read_bytes()
            except OSError:
                logger.exception("orchestrator.artifact_input_read_failed path=%s", resolved_path)
                content = None
        if content is None:
            remote_url = str(artifact.get("provider_url") or artifact.get("download_url") or "").strip()
            if remote_url:
                try:
                    response = await self._client.get(remote_url)
                    response.raise_for_status()
                    content = response.content
                    if response.headers.get("content-type"):
                        mime_type = str(response.headers.get("content-type") or "").split(";", 1)[0].strip() or mime_type
                except Exception:
                    logger.exception("orchestrator.artifact_input_fetch_failed url=%s", remote_url)
                    content = None
        if not content:
            return None
        max_bytes = max(1024, int(self.config.anthropic_max_staged_input_file_bytes))
        if len(content) > max_bytes:
            logger.info(
                "orchestrator.artifact_input_skipped_too_large artifact_id=%s size=%s max=%s",
                str(artifact.get("artifact_id") or "").strip(),
                len(content),
                max_bytes,
            )
            return None
        return InputArtifactPayload(
            artifact=dict(artifact),
            filename=filename,
            mime_type=mime_type,
            content=content,
        )

    def _resolve_logical_artifact_input_path(self, logical_path: str) -> Path | None:
        value = str(logical_path or "").strip()
        if not value:
            return None
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            return candidate
        normalized = value.replace("\\", "/").lstrip("./")
        if normalized.startswith("runs/artifacts/"):
            relative = normalized[len("runs/artifacts/") :].strip("/")
            if relative:
                return (self.config.artifacts_root / Path(relative)).resolve()
        return (BACKEND_ROOT / Path(normalized)).resolve()

    async def _upload_input_artifact_to_anthropic(self, payload: InputArtifactPayload) -> str | None:
        artifact_id = str(payload.artifact.get("artifact_id") or "").strip()
        sha256 = str(payload.artifact.get("sha256") or hashlib.sha256(payload.content).hexdigest()).strip()
        key = (artifact_id, sha256, payload.filename)
        cached = self._anthropic_input_file_cache.get(key)
        if cached:
            return cached
        headers = self._anthropic_api_headers()
        try:
            response = await self._client.post(
                "https://api.anthropic.com/v1/files",
                headers=headers,
                files={"file": (payload.filename, payload.content, payload.mime_type)},
            )
            response.raise_for_status()
            data = response.json()
        except Exception:
            logger.exception(
                "orchestrator.anthropic_input_file_upload_failed artifact_id=%s filename=%s",
                artifact_id,
                payload.filename,
            )
            return None
        if not isinstance(data, dict):
            return None
        file_id = str(data.get("id") or "").strip()
        if not file_id:
            return None
        self._anthropic_input_file_cache[key] = file_id
        return file_id

    def _prepare_messages_for_anthropic(
        self,
        messages: list[dict[str, Any]],
        *,
        strip_all_server_tool_blocks: bool = False,
        strip_thinking_blocks: bool = False,
    ) -> list[dict[str, Any]]:
        """Build a replay-safe, canonicalized Anthropic messages list.

        This runs before every Anthropic request, not just the immediate pause_turn
        continuation path. It protects against malformed prior assistant block lists
        and collapses consecutive same-role messages that can accumulate across
        repeated internal tool loops.
        """
        candidate_messages: list[tuple[int, dict[str, Any]]] = [
            (index, item)
            for index, item in enumerate(messages)
            if isinstance(item, dict)
            and str(item.get("role") or "").strip() in {"user", "assistant"}
        ]
        last_candidate_index = candidate_messages[-1][0] if candidate_messages else -1

        prepared: list[dict[str, Any]] = []
        for original_index, item in candidate_messages:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            if role not in {"user", "assistant"}:
                continue
            raw_content = item.get("content")
            if role == "assistant" and isinstance(raw_content, list):
                # Server-side tool blocks are only replay-safe for the immediate
                # trailing assistant continuation. Older conversation history can
                # look structurally paired but still be rejected by Anthropic as
                # stale server-tool replay.
                allow_server_tool_replay = original_index == last_candidate_index
                if strip_all_server_tool_blocks or not allow_server_tool_replay:
                    content, _ = self._strip_server_tool_replay_content_blocks(
                        raw_content
                    )
                else:
                    content, _ = self._sanitize_server_tool_replay_content_blocks(
                        raw_content
                    )
                if strip_thinking_blocks:
                    content, _ = self._strip_thinking_replay_content_blocks(content)
            else:
                content = self._normalize_message_content(raw_content)
            if self._message_content_is_empty(content):
                continue
            if prepared and prepared[-1]["role"] == role:
                prepared[-1]["content"] = self._merge_message_content(prepared[-1]["content"], content)
                continue
            prepared.append({"role": role, "content": content})

        while prepared and prepared[0]["role"] != "user":
            prepared.pop(0)
        return prepared

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

    @staticmethod
    def _merge_usage(existing: dict[str, Any], usage: Any) -> dict[str, Any]:
        if not isinstance(usage, dict):
            return existing
        merged = dict(existing)
        for key, value in usage.items():
            if isinstance(value, (int, float)):
                merged[key] = merged.get(key, 0) + int(value)
        return merged

    @staticmethod
    def _merge_stream_usage(existing: dict[str, Any], usage: Any) -> dict[str, Any]:
        """Merge usage chunks from within ONE streamed response.

        Usage fields on a stream are cumulative: providers either send a full
        usage object on the final chunk or a running total on many chunks, so
        the true value per field is the running maximum. Summing instead of
        taking the maximum inflated turns whose provider streams usage on
        every chunk (GLM 5.3 Flash reported ~180M prompt tokens for a single
        ~1M-context request). Cross-response totals - one merge per completed
        API call, e.g. tool iterations - still use _merge_usage, which sums.
        """
        if not isinstance(usage, dict):
            return existing
        merged = dict(existing)
        for key, value in usage.items():
            if isinstance(value, (int, float)):
                merged[key] = max(merged.get(key, 0), int(value))
        return merged

    @staticmethod
    def _append_stream_text(current: str, incoming: str) -> str:
        prev = str(current or "")
        next_text = str(incoming or "")
        if not next_text:
            return prev
        if not prev:
            return next_text

        prev_end = prev[-1:]
        next_start = next_text[:1]
        if not prev_end or not next_start or prev_end.isspace() or next_start.isspace():
            return prev + next_text
        if re.search(r'[\.\!\?:\u2026]', prev_end) and re.search(
            r'[A-Za-z0-9"\'`\(\[]',
            next_start,
        ):
            return prev + "\n\n" + next_text
        if re.search(r"[A-Za-z0-9]", prev_end) and re.search(r"[A-Za-z0-9]", next_start):
            return prev + " " + next_text
        return prev + next_text

    @staticmethod
    def _stream_turn_chunk_delta(
        current: str,
        incoming: str,
        *,
        boundary_emitted: bool,
    ) -> tuple[str, bool]:
        chunk = str(incoming or "")
        if not chunk:
            return "", boundary_emitted
        current_text = str(current or "")
        if boundary_emitted or not current_text:
            return chunk, True
        merged = OrchestratorRuntime._append_stream_text(current_text, chunk)
        if merged.startswith(current_text):
            return merged[len(current_text):], True
        return chunk, True

    async def _emit_turn_segment_boundary(
        self,
        ev: dict[str, Any],
        *,
        visual_coordinator: Any | None,
        iteration: int,
        tools_called: list[str],
    ) -> AsyncIterator[dict[str, Any]]:
        """Paragraph break between two narration turns.

        The break MUST flow through the visual coordinator when one is active:
        the coordinator's markdown accumulation feeds the response-block
        snapshots and the completion content, so a break injected around it
        reaches the live text stream but silently vanishes from the blocks and
        from the final persisted message."""
        yield {
            **ev,
            "type": "response.segment",
            "reason": "tool_call",
            "iteration": iteration,
            "tools_called": tools_called,
        }
        if visual_coordinator is not None:
            visible_break, visual_events = visual_coordinator.consume_text("\n\n")
            for visual_event in visual_events:
                yield {**ev, **visual_event}
            if visible_break:
                yield {**ev, "type": "response.chunk", "content": visible_break, "done": False}
        else:
            yield {**ev, "type": "response.chunk", "content": "\n\n", "done": False}

    def _estimate_request_context_chars(self, system_prompt: str, messages: list[dict[str, Any]]) -> int:
        try:
            messages_json = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            messages_json = repr(messages)
        return len(system_prompt) + len(messages_json)

    @staticmethod
    def _expected_server_tool_result_type(tool_name: str) -> str | None:
        normalized = str(tool_name or "").strip()
        if not normalized:
            return None
        return f"{normalized}_tool_result"

    @staticmethod
    def _server_tool_result_has_replay_payload(
        block: dict[str, Any],
        *,
        tool_name: str | None = None,
    ) -> bool:
        """
        Require enough payload for a replayed server tool result to be valid.

        A partially streamed *_tool_result block can include only bookkeeping
        keys like `type` and `tool_use_id`. Replaying that malformed result on
        the next Anthropic request can cause the continuation to fail hard.
        """
        if not isinstance(block, dict):
            return False

        normalized_tool = str(tool_name or "").strip()
        if normalized_tool in {"code_execution", "web_search", "web_fetch"}:
            return "content" in block
        if normalized_tool == "bash_code_execution":
            return any(key in block for key in ("content", "stdout", "stderr", "exit_code", "return_code"))

        for key, value in block.items():
            if key in {"type", "tool_use_id", "cache_control"}:
                continue
            if value is not None:
                return True
        return False

    @staticmethod
    def _append_server_tool_skip_note(message: str) -> str:
        message = str(message or "").rstrip()
        if message:
            if message[-1] not in ".!?":
                message += "."
            message += " "
        return message + "Some incomplete server-side tool blocks were skipped to keep the continuation valid."

    @staticmethod
    def _is_unmatched_server_tool_replay_error(exc: BaseException) -> bool:
        normalized = str(exc or "").strip().lower()
        if not normalized:
            return False
        return (
            "tool use with id" in normalized
            and "without a corresponding" in normalized
            and ("_tool_result block" in normalized or "tool_result" in normalized)
        )

    @staticmethod
    def _is_modified_thinking_replay_error(exc: BaseException) -> bool:
        normalized = str(exc or "").strip().lower()
        if not normalized:
            return False
        return (
            ("thinking" in normalized or "redacted_thinking" in normalized)
            and "latest assistant message" in normalized
            and "cannot be modified" in normalized
        )

    def _strip_server_tool_replay_content_blocks(
        self,
        content_blocks: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], bool]:
        sanitized: list[dict[str, Any]] = []
        dropped_any = False
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "").strip()
            if (
                block_type == "server_tool_use"
                or ContentBlock._is_server_tool_result_block(block_type)
            ):
                dropped_any = True
                continue
            sanitized.append(dict(block))
        return sanitized, dropped_any

    def _strip_thinking_replay_content_blocks(
        self,
        content_blocks: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], bool]:
        sanitized: list[dict[str, Any]] = []
        dropped_any = False
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "").strip()
            if block_type in {"thinking", "redacted_thinking"}:
                dropped_any = True
                continue
            sanitized.append(dict(block))
        return sanitized, dropped_any

    def _sanitize_server_tool_replay_content_blocks(
        self,
        content_blocks: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], bool]:
        """
        Build replay-safe assistant content for Anthropic pause_turn continuation.

        Anthropic expects each server_tool_use block to be paired with the
        corresponding *_tool_result block on the next request. If the streamed
        turn is ever incomplete or malformed, replaying an unmatched server tool
        block causes the next request to fail hard. We therefore only echo back
        fully paired server-side tool blocks and preserve all non-server blocks.
        """
        server_use_by_id: dict[str, dict[str, Any]] = {}
        server_result_by_tool_use_id: dict[str, dict[str, Any]] = {}
        normalized_blocks = [block for block in content_blocks if isinstance(block, dict)]
        for block in normalized_blocks:
            block_type = str(block.get("type") or "").strip()
            if block_type == "server_tool_use":
                tool_id = str(block.get("id") or "").strip()
                if tool_id:
                    server_use_by_id[tool_id] = block
                continue
            if ContentBlock._is_server_tool_result_block(block_type):
                tool_use_id = str(block.get("tool_use_id") or "").strip()
                if tool_use_id:
                    server_result_by_tool_use_id[tool_use_id] = block

        sanitized: list[dict[str, Any]] = []
        dropped_any = False

        for block in normalized_blocks:
            block_type = str(block.get("type") or "").strip()
            if block_type == "server_tool_use":
                tool_id = str(block.get("id") or "").strip()
                tool_name = str(block.get("name") or "").strip()
                match = server_result_by_tool_use_id.get(tool_id)
                expected_type = self._expected_server_tool_result_type(tool_name)
                if (
                    not match
                    or (expected_type and str(match.get("type") or "").strip() != expected_type)
                    or not self._server_tool_result_has_replay_payload(match, tool_name=tool_name)
                ):
                    dropped_any = True
                    continue
                sanitized.append(dict(block))
                continue

            if ContentBlock._is_server_tool_result_block(block_type):
                tool_use_id = str(block.get("tool_use_id") or "").strip()
                matched_use = server_use_by_id.get(tool_use_id)
                tool_name = str(matched_use.get("name") or "").strip() if matched_use else ""
                expected_type = self._expected_server_tool_result_type(tool_name) if matched_use else None
                if (
                    not matched_use
                    or (expected_type and block_type != expected_type)
                    or not self._server_tool_result_has_replay_payload(block, tool_name=tool_name)
                ):
                    dropped_any = True
                    continue
                sanitized.append(dict(block))
                continue

            sanitized.append(dict(block))

        return sanitized, dropped_any

    def _sanitize_server_tool_replay_blocks(
        self,
        blocks: dict[int, ContentBlock],
    ) -> tuple[list[dict[str, Any]], bool]:
        content_blocks = [blocks[idx].to_api_dict() for idx in sorted(blocks)]
        return self._sanitize_server_tool_replay_content_blocks(content_blocks)

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
    def _normalize_anthropic_error_text(message: str) -> str:
        normalized = str(message or "").strip()
        if normalized.lower().startswith("anthropic api error:"):
            normalized = normalized.split(":", 1)[1].strip()
        return normalized or "Anthropic API error"

    def _is_transient_anthropic_overload(self, exc: BaseException) -> bool:
        message = self._normalize_anthropic_error_text(str(exc)).lower()
        if not message:
            return False
        if re.search(r"\bstatus=(429|503|529)\b", message):
            return True
        overload_markers = (
            "overloaded",
            "overload",
            "capacity",
            "rate limit",
            "too many requests",
            "temporarily unavailable",
            "server overloaded",
        )
        return any(marker in message for marker in overload_markers)

    def _anthropic_overload_backoff_sec(self, retry_count: int) -> float:
        delay = self.config.anthropic_overload_initial_backoff_sec * (2 ** max(0, retry_count))
        return min(delay, self.config.anthropic_overload_max_backoff_sec)

    def _plan_anthropic_turn_retry(
        self,
        *,
        exc: BaseException,
        retry_count: int,
        responding_announced: bool,
        server_tool_progress_emitted: bool,
        active_model: str,
        fallback_used: bool,
    ) -> AnthropicRetryPlan | None:
        if not self._is_transient_anthropic_overload(exc):
            return None
        if responding_announced or server_tool_progress_emitted:
            return None
        if retry_count < self.config.anthropic_overload_retry_attempts:
            return AnthropicRetryPlan(
                message="Opus hit temporary capacity. Retrying automatically...",
                backoff_sec=self._anthropic_overload_backoff_sec(retry_count),
            )
        fallback_model = str(self.config.anthropic_overload_fallback_model or "").strip()
        if fallback_model and not fallback_used and fallback_model != active_model:
            return AnthropicRetryPlan(
                message="Opus hit temporary capacity. Retrying with a standby model...",
                backoff_sec=self._anthropic_overload_backoff_sec(retry_count),
                model_override=fallback_model,
            )
        return None

    def _normalize_anthropic_turn_error(self, exc: BaseException) -> BaseException:
        if isinstance(exc, OrchestratorTaskError):
            return exc
        if self._is_transient_anthropic_overload(exc):
            return OrchestratorTaskError(
                "Opus is temporarily overloaded right now. Please try again in a moment.",
                code="OPUS_TEMPORARILY_OVERLOADED",
                retryable=True,
            )
        return exc

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
        if data.get("error") is True:
            # A delegation that failed is part of the story of a request. Recording
            # only the successful ones is what made a specialist reroute invisible
            # in the trace even though the model had explained it out loud.
            receipt["failed"] = True
            error_detail: dict[str, Any] = {}
            code = self._activity_excerpt(data.get("code"), limit=48)
            message = self._activity_excerpt(data.get("message"), limit=240)
            if code:
                error_detail["code"] = code
            if message:
                error_detail["message"] = message
            if data.get("in_progress") is True:
                error_detail["in_progress"] = True
            delegation_dispatched = delegation.get("dispatched")
            if delegation_dispatched is False:
                error_detail["dispatched"] = False
            if error_detail:
                receipt["error"] = error_detail
        provider = self._activity_excerpt(data.get("provider"), limit=48)
        model = self._activity_excerpt(data.get("model"), limit=96)
        if provider:
            receipt["provider"] = provider
        if model:
            receipt["model"] = model
        fallback_from = data.get("fallback_from") if isinstance(data.get("fallback_from"), dict) else {}
        if fallback_from:
            fallback_provider = self._activity_excerpt(fallback_from.get("provider"), limit=48)
            fallback_model = self._activity_excerpt(fallback_from.get("model"), limit=96)
            if fallback_provider or fallback_model:
                receipt["fallback_from"] = {
                    key: value
                    for key, value in {
                        "provider": fallback_provider,
                        "model": fallback_model,
                    }.items()
                    if value
                }
        retry_from = data.get("retry_from") if isinstance(data.get("retry_from"), dict) else {}
        if retry_from:
            retry_provider = self._activity_excerpt(retry_from.get("provider"), limit=48)
            retry_model = self._activity_excerpt(retry_from.get("model"), limit=96)
            if retry_provider or retry_model:
                receipt["retry_from"] = {
                    key: value
                    for key, value in {
                        "provider": retry_provider,
                        "model": retry_model,
                    }.items()
                    if value
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
        if intent_name == "gmail.draft_reply":
            draft = data.get("draft") if isinstance(data.get("draft"), dict) else {}
            account = data.get("account") if isinstance(data.get("account"), dict) else {}
            message = data.get("message") if isinstance(data.get("message"), dict) else {}

            def _string_list(value: Any) -> list[str]:
                if isinstance(value, str):
                    values = [value]
                elif isinstance(value, list):
                    values = value
                else:
                    values = []
                out: list[str] = []
                for raw in values:
                    text = self._activity_excerpt(raw, limit=320)
                    if text:
                        out.append(text)
                return out[:25]

            gmail_approval = {
                key: value
                for key, value in {
                    "account_id": self._activity_excerpt(account.get("account_id"), limit=160),
                    "account_email": self._activity_excerpt(account.get("account_email"), limit=320),
                    "account_label": self._activity_excerpt(account.get("account_label"), limit=320),
                    "draft_id": self._activity_excerpt(data.get("draft_id"), limit=160),
                    "message_id": self._activity_excerpt(message.get("id"), limit=160),
                    "thread_id": self._activity_excerpt(
                        message.get("threadId") or message.get("thread_id"), limit=160
                    ),
                    "subject": self._activity_excerpt(draft.get("subject"), limit=500),
                    "body_text": self._activity_excerpt(draft.get("body"), limit=6000),
                    "body_preview": self._activity_excerpt(draft.get("body"), limit=700),
                    "notes": self._activity_excerpt(data.get("notes"), limit=700),
                }.items()
                if value
            }
            for key in ("to", "cc", "bcc"):
                values = _string_list(draft.get(key))
                if values:
                    gmail_approval[key] = values
            if gmail_approval.get("account_id") and gmail_approval.get("draft_id"):
                receipt["gmail_approval"] = gmail_approval
        if intent_name in {
            "calendar.create_event",
            "calendar.update_event",
            "calendar.respond_to_invite",
            "calendar.cancel_event",
        }:
            event = data.get("event") if isinstance(data.get("event"), dict) else {}
            account = data.get("account") if isinstance(data.get("account"), dict) else {}
            if event:
                operation = {
                    "calendar.create_event": "created",
                    "calendar.update_event": "updated",
                    "calendar.respond_to_invite": self._activity_excerpt(
                        data.get("response_status"), limit=80
                    )
                    or "responded",
                    "calendar.cancel_event": "cancelled",
                }[intent_name]
                calendar_event = {
                    key: value
                    for key, value in {
                        "operation": operation,
                        "event_id": self._activity_excerpt(event.get("event_id"), limit=180),
                        "calendar_id": self._activity_excerpt(event.get("calendar_id"), limit=320),
                        "summary": self._activity_excerpt(event.get("summary"), limit=500),
                        "description": self._activity_excerpt(event.get("description"), limit=1200),
                        "location": self._activity_excerpt(event.get("location"), limit=500),
                        "start": self._activity_excerpt(event.get("start"), limit=160),
                        "end": self._activity_excerpt(event.get("end"), limit=160),
                        "status": self._activity_excerpt(event.get("status"), limit=80),
                        "response_status": self._activity_excerpt(
                            data.get("response_status"), limit=80
                        ),
                        "html_link": self._activity_excerpt(event.get("html_link"), limit=1200),
                        "meeting_link": self._activity_excerpt(event.get("meeting_link"), limit=1200),
                        "organizer": self._activity_excerpt(event.get("organizer"), limit=320),
                        "is_all_day": bool(event.get("is_all_day")),
                        "attendees": event.get("attendees")
                        if isinstance(event.get("attendees"), list)
                        else [],
                        "account": {
                            key: value
                            for key, value in {
                                "account_id": self._activity_excerpt(account.get("account_id"), limit=160),
                                "email": self._activity_excerpt(account.get("email"), limit=320),
                                "account_label": self._activity_excerpt(account.get("account_label"), limit=320),
                            }.items()
                            if value
                        },
                    }.items()
                    if value not in (None, "", [], {})
                }
                if calendar_event.get("event_id") or calendar_event.get("summary"):
                    receipt["calendar_event"] = calendar_event
        if intent_name == "alpha.execute":
            project = data.get("project") if isinstance(data.get("project"), dict) else {}

            def _find_opportunity_id(value: Any) -> str:
                if isinstance(value, dict):
                    direct = self._activity_excerpt(
                        value.get("tool_opportunity_id") or value.get("opportunity_id"),
                        limit=160,
                    )
                    if direct:
                        return direct
                    for nested in value.values():
                        found = _find_opportunity_id(nested)
                        if found:
                            return found
                elif isinstance(value, list):
                    for nested in value:
                        found = _find_opportunity_id(nested)
                        if found:
                            return found
                elif isinstance(value, str):
                    match = re.search(r"\btool_[a-f0-9]{8,32}\b", value, re.IGNORECASE)
                    if match:
                        return match.group(0)
                return ""

            opportunity_id = _find_opportunity_id(tool_input)
            alpha_project = {
                key: value
                for key, value in {
                    "tool_opportunity_id": opportunity_id,
                    "alpha_project_id": self._activity_excerpt(project.get("project_id"), limit=200),
                    "status": self._activity_excerpt(project.get("status") or data.get("status"), limit=80),
                    "repo_url": self._activity_excerpt(project.get("repo_url"), limit=1200),
                    "deployment_url": self._activity_excerpt(project.get("deployment_url"), limit=1200),
                    "last_task_id": self._activity_excerpt(project.get("last_task_id"), limit=200),
                }.items()
                if value
            }
            if alpha_project.get("alpha_project_id"):
                receipt["alpha_project"] = alpha_project
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
        self._trim_specialist_receipts(specialist_receipts)

    def _collect_sandbox_permission_receipt(
        self,
        tool_name: str,
        result_str: str,
        *,
        specialist_receipts: list[dict[str, Any]],
    ) -> None:
        if tool_name != "cosmic_code_execution":
            return
        data = self._parse_tool_result_json(result_str)
        if not isinstance(data, dict) or not data.get("permission_required"):
            return
        permission = data.get("sandbox_permission")
        if not isinstance(permission, dict):
            return
        permission_id = str(permission.get("permission_id") or "").strip()
        if not permission_id:
            return
        existing_ids = {
            str(item.get("sandbox_permission", {}).get("permission_id") or "").strip()
            for item in specialist_receipts
            if isinstance(item, dict) and isinstance(item.get("sandbox_permission"), dict)
        }
        if permission_id in existing_ids:
            return
        specialist_receipts.append({"sandbox_permission": permission})
        self._trim_specialist_receipts(specialist_receipts)

    # Receipts are a narrative summary, so the list is capped. Some of them are not
    # summaries at all: the gateway persists a pending Gmail approval FROM the
    # receipt that carries it, so trimming one away means the draft exists in the
    # user's mailbox but never reaches the approval queue. Those are kept.
    _RECEIPT_SOFT_CAP = 4
    _RECEIPT_HARD_CAP = 8
    _CONSEQUENTIAL_RECEIPT_KEYS = (
        "gmail_approval",
        "sandbox_permission",
        "calendar_event",
        "alpha_project",
    )

    @classmethod
    def _receipt_is_consequential(cls, receipt: Any) -> bool:
        if not isinstance(receipt, dict):
            return False
        return any(
            isinstance(receipt.get(key), dict) for key in cls._CONSEQUENTIAL_RECEIPT_KEYS
        )

    @classmethod
    def _trim_specialist_receipts(cls, specialist_receipts: list[dict[str, Any]]) -> None:
        """Cap the receipt list without discarding the receipts that carry state.

        Newest wins within each group. Receipts that create something the user can
        act on are kept up to a hard ceiling even when that exceeds the soft cap --
        an oversized payload is a smaller problem than an approval card that never
        appears for a draft that already exists.
        """
        if len(specialist_receipts) <= cls._RECEIPT_SOFT_CAP:
            return
        consequential = [
            index
            for index, item in enumerate(specialist_receipts)
            if cls._receipt_is_consequential(item)
        ]
        keep = set(consequential[-cls._RECEIPT_HARD_CAP:])
        remaining = cls._RECEIPT_SOFT_CAP - len(keep)
        if remaining > 0:
            narrative = [
                index for index in range(len(specialist_receipts)) if index not in keep
            ]
            keep.update(narrative[-remaining:])
        specialist_receipts[:] = [
            item for index, item in enumerate(specialist_receipts) if index in keep
        ]

    def _collect_specialist_artifacts(
        self,
        result_str: str,
        *,
        produced_artifacts: list[dict[str, Any]],
        supporting_artifacts: list[dict[str, Any]] | None = None,
    ) -> None:
        data = self._parse_tool_result_json(result_str)
        if not isinstance(data, dict):
            return
        raw_artifacts = data.get("artifacts")
        if not isinstance(raw_artifacts, list):
            return

        produced_keys = {
            (
                str(item.get("artifact_id") or "").strip(),
                str(item.get("path") or "").strip(),
            )
            for item in produced_artifacts
            if isinstance(item, dict)
        }
        supporting_keys = {
            (
                str(item.get("artifact_id") or "").strip(),
                str(item.get("path") or "").strip(),
            )
            for item in (supporting_artifacts or [])
            if isinstance(item, dict)
        }
        for item in raw_artifacts:
            if not isinstance(item, dict):
                continue
            audience = self._activity_excerpt(item.get("audience"), limit=32) or "deliverable"
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

            artifact_payload = {
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

            if audience != "deliverable":
                if supporting_artifacts is None or dedupe_key in supporting_keys:
                    continue
                supporting_keys.add(dedupe_key)
                supporting_artifacts.append(artifact_payload)
                if len(supporting_artifacts) >= 24:
                    break
                continue

            if dedupe_key in produced_keys:
                continue
            produced_keys.add(dedupe_key)
            produced_artifacts.append(artifact_payload)
            if len(produced_artifacts) >= 12:
                break

    async def _collect_server_tool_artifacts(
        self,
        blocks: list[ContentBlock],
        *,
        task: TaskEnvelope,
        produced_artifacts: list[dict[str, Any]],
    ) -> None:
        existing_keys = {
            (
                str(item.get("artifact_id") or "").strip(),
                str(item.get("path") or "").strip(),
            )
            for item in produced_artifacts
            if isinstance(item, dict)
        }
        for block in blocks:
            if not ContentBlock._is_server_tool_result_block(block.block_type):
                continue
            raw_block = block.raw_block if isinstance(block.raw_block, dict) else None
            if not raw_block:
                continue
            for file_id in self._extract_server_tool_file_ids(raw_block):
                artifact_id = f"anthropic_{file_id}"
                if any(key[0] == artifact_id for key in existing_keys):
                    continue
                try:
                    artifact = await self._download_anthropic_generated_file(
                        file_id=file_id,
                        task_id=task.task_id,
                    )
                except Exception:
                    logger.exception(
                        "orchestrator.anthropic_generated_file_capture_failed task_id=%s file_id=%s",
                        task.task_id,
                        file_id,
                    )
                    continue
                if not artifact:
                    continue
                dedupe_key = (
                    str(artifact.get("artifact_id") or "").strip(),
                    str(artifact.get("path") or "").strip(),
                )
                if not any(dedupe_key) or dedupe_key in existing_keys:
                    continue
                existing_keys.add(dedupe_key)
                produced_artifacts.append(artifact)
                if len(produced_artifacts) >= 12:
                    return

    def _extract_server_tool_file_ids(self, raw_block: dict[str, Any]) -> list[str]:
        discovered: list[str] = []
        seen: set[str] = set()

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                raw_file_id = value.get("file_id")
                if isinstance(raw_file_id, str):
                    file_id = raw_file_id.strip()
                    if file_id.startswith("file_") and file_id not in seen:
                        seen.add(file_id)
                        discovered.append(file_id)
                for nested in value.values():
                    walk(nested)
                return
            if isinstance(value, list):
                for item in value:
                    walk(item)

        walk(raw_block)
        return discovered

    async def _download_anthropic_generated_file(
        self,
        *,
        file_id: str,
        task_id: str,
    ) -> dict[str, Any] | None:
        headers = self._anthropic_api_headers()
        metadata_response = await self._client.get(
            f"https://api.anthropic.com/v1/files/{file_id}",
            headers=headers,
        )
        metadata_response.raise_for_status()
        metadata = metadata_response.json()
        if not isinstance(metadata, dict):
            return None

        content_response = await self._client.get(
            f"https://api.anthropic.com/v1/files/{file_id}/content",
            headers=self._anthropic_api_headers(accept="application/octet-stream"),
        )
        content_response.raise_for_status()
        content = content_response.content
        if not content:
            return None

        mime_type = (
            str(metadata.get("mime_type") or "").strip()
            or str(content_response.headers.get("content-type") or "").split(";", 1)[0].strip()
            or "application/octet-stream"
        )
        default_filename = self._default_generated_filename(file_id=file_id, mime_type=mime_type)
        filename = self._sanitize_generated_filename(
            str(metadata.get("filename") or "").strip(),
            fallback=default_filename,
        )
        task_dir = self.config.artifacts_root / task_id / "orchestrator" / "anthropic_code_execution"
        task_dir.mkdir(parents=True, exist_ok=True)
        destination = task_dir / f"{file_id}__{filename}"
        destination.write_bytes(content)
        sha256 = hashlib.sha256(content).hexdigest()
        return {
            "artifact_id": f"anthropic_{file_id}",
            "task_id": task_id,
            "mime": mime_type,
            "path": self._logical_artifact_path(destination),
            "kind": "output",
            "audience": "deliverable",
            "filename": filename,
            "created_by_agent": self.config.orchestrator_agent_id,
            "sha256": sha256,
            "created_at": metadata.get("created_at"),
        }

    def _anthropic_api_headers(self, *, accept: str = "application/json", include_code_execution_beta: bool = False) -> dict[str, str]:
        headers = {
            "x-api-key": self.config.anthropic_api_key,
            "anthropic-version": self.config.anthropic_version,
            "accept": accept,
        }
        betas: list[str] = []
        files_beta = str(self.config.anthropic_files_api_beta or "").strip()
        if files_beta:
            betas.append(files_beta)
        if include_code_execution_beta:
            code_beta = str(self.config.anthropic_code_execution_beta or "").strip()
            if code_beta:
                betas.insert(0, code_beta)
        if betas:
            headers["anthropic-beta"] = ",".join(self._dedupe_preserve_order(betas))
        return headers

    def _default_generated_filename(self, *, file_id: str, mime_type: str) -> str:
        extension = mimetypes.guess_extension(mime_type or "") or ""
        safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", file_id).strip("._") or "generated"
        return f"{safe_id}{extension}"

    def _sanitize_generated_filename(self, filename: str, *, fallback: str) -> str:
        candidate = Path(filename or "").name.strip()
        if not candidate:
            candidate = fallback
        candidate = re.sub(r"[\x00-\x1f]+", "_", candidate)
        candidate = re.sub(r"[^A-Za-z0-9._() \-]+", "_", candidate).strip(" ._")
        return candidate or fallback

    def _logical_artifact_path(self, path: Path) -> str:
        resolved = path.resolve()
        relative = resolved.relative_to(self.config.artifacts_root.resolve())
        return (Path("runs") / "artifacts" / relative).as_posix()

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

    def _extract_specialist_delegations(
        self,
        tool_blocks: list[ContentBlock],
        parsed_inputs: list[dict[str, Any]],
        result_strs: list[str],
    ) -> list[dict[str, Any]]:
        delegations: list[dict[str, Any]] = []
        for block, tool_input, result_str in zip(tool_blocks, parsed_inputs, result_strs):
            if block.tool_name != "delegate_to_agent":
                continue
            data = self._parse_tool_result_json(result_str)
            delegation = data.get("delegation") if isinstance(data, dict) and isinstance(data.get("delegation"), dict) else {}
            intent_name = self._activity_excerpt(
                delegation.get("intent") or tool_input.get("intent"),
                limit=96,
            )
            agent_id = self._activity_excerpt(
                delegation.get("agent_id") or tool_input.get("agent_id"),
                limit=120,
            )
            task_id = self._activity_excerpt(
                delegation.get("task_id")
                or (data.get("delegated_task_id") if isinstance(data, dict) else None)
                or (data.get("task_id") if isinstance(data, dict) else None),
                limit=96,
            )
            agent_label = self._activity_agent_label(agent_id)
            activity = self._activity_excerpt(
                self._summarize_local_tool_activity(block.tool_name, tool_input, result_str),
                limit=160,
            )
            if not (intent_name or agent_id or task_id):
                continue
            delegations.append(
                {
                    key: value
                    for key, value in {
                        "intent": intent_name,
                        "agent_id": agent_id,
                        "agent_label": agent_label,
                        "task_id": task_id,
                        "activity": activity,
                    }.items()
                    if value not in (None, "")
                }
            )
        return delegations

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

        if tool_name == "cosmic_code_execution":
            if isinstance(data, dict):
                status_value = self._activity_excerpt(data.get("status"), limit=32)
                artifact_count = data.get("artifact_count")
                if status_value == "completed" and isinstance(artifact_count, int) and artifact_count > 0:
                    return f"ran the local code sandbox and produced {artifact_count} file(s)"
                if status_value == "completed":
                    return "ran the local code sandbox"
                if data.get("timed_out"):
                    return "ran the local code sandbox and it timed out"
                if status_value:
                    return f"ran the local code sandbox and it {status_value}"
            return "ran the local code sandbox"

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
