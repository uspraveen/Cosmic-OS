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
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from uuid import uuid4

import httpx
import redis.asyncio as redis

from gateway.adapters.response_processor import AWAITING_REPLY_TAG
from gateway.adapters.response_processor import normalize_conversation_history
from shared import TaskEnvelope, create_redis_client, ensure_stream_group, parse_stream_payload, verify_task_envelope

from .config import OrchestratorConfig
from .prompts import build_agentic_system_prompt
from .store.ledger import TaskLedger
from .tools.definitions import get_tool_definitions
from .tools.executor import ToolExecutor

logger = logging.getLogger(__name__)


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
    # Tool use
    tool_id: str = ""
    tool_name: str = ""
    input_json: str = ""

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
        return {"type": self.block_type}


@dataclass(slots=True)
class ActiveTaskRun:
    runner_task: asyncio.Task[Any] | None
    request_id: str | None
    session_id: str | None
    channel: str | None
    cancel_requested: bool = False
    cancel_message: str = "Response stopped."


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
        self.started = False
        self._active_runs: dict[str, ActiveTaskRun] = {}
        self._pending_input_futures: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._reply_consumer_task: asyncio.Task[None] | None = None
        self._tool_executor: ToolExecutor | None = None

    async def start(self) -> None:
        self.task_ledger.initialize()
        if self._redis is not None:
            await ensure_stream_group(
                self._redis,
                stream=self.config.task_input_replies_stream,
                group=self.config.task_input_orchestrator_group,
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
        if self._reply_consumer_task is not None:
            self._reply_consumer_task.cancel()
            await asyncio.gather(self._reply_consumer_task, return_exceptions=True)
            self._reply_consumer_task = None
        for future in list(self._pending_input_futures.values()):
            if not future.done():
                future.cancel()
        self._pending_input_futures.clear()
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

        try:
            messages = self._build_messages(task)
            system_prompt = build_agentic_system_prompt(
                str(task.input.get("memory_context") or "").strip() or None
            )
            tools = get_tool_definitions()
            max_iterations = self.config.max_tool_iterations

            iteration = 0
            full_response_text = ""
            full_reasoning_text = ""

            while iteration < max_iterations:
                iteration += 1

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
                ):
                    if sse.event == "ping" or not sse.data:
                        continue
                    payload = json.loads(sse.data)
                    ptype = str(payload.get("type") or "")

                    # ── message_start ───────────────────────────
                    if ptype == "message_start":
                        turn_usage = self._merge_usage(turn_usage, payload.get("message", {}).get("usage"))
                        continue

                    # ── message_delta ───────────────────────────
                    if ptype == "message_delta":
                        turn_usage = self._merge_usage(turn_usage, payload.get("usage"))
                        delta = payload.get("delta")
                        if isinstance(delta, dict):
                            turn_stop_reason = str(delta.get("stop_reason") or "").strip() or turn_stop_reason
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
                        blocks[idx] = block
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

                    # content_block_stop — nothing to capture beyond what deltas provided
                    # message_stop — nothing to do

                # ── End of Anthropic turn ───────────────────────
                cumulative_usage = self._merge_usage(cumulative_usage, turn_usage)
                stop_reason = turn_stop_reason

                # Collect text and reasoning from this turn
                turn_text_parts: list[str] = []
                turn_reasoning_parts: list[str] = []
                turn_tool_blocks: list[ContentBlock] = []
                for idx in sorted(blocks):
                    b = blocks[idx]
                    if b.block_type == "thinking" and b.thinking_text:
                        turn_reasoning_parts.append(b.thinking_text)
                    elif b.block_type == "text" and b.text:
                        turn_text_parts.append(b.text)
                    elif b.block_type == "tool_use":
                        turn_tool_blocks.append(b)

                turn_text = "".join(turn_text_parts)
                turn_reasoning = "".join(turn_reasoning_parts)
                full_response_text += turn_text
                full_reasoning_text += turn_reasoning

                # ── Tool use → execute and loop ─────────────────
                if turn_stop_reason == "tool_use" and turn_tool_blocks:
                    # Reconstruct the full assistant message (thinking + text + tool_use)
                    assistant_content = [blocks[idx].to_api_dict() for idx in sorted(blocks)]
                    messages.append({"role": "assistant", "content": assistant_content})

                    # Execute each tool
                    tool_results: list[dict[str, Any]] = []
                    for tb in turn_tool_blocks:
                        try:
                            parsed_input = json.loads(tb.input_json) if tb.input_json else {}
                        except json.JSONDecodeError:
                            parsed_input = {}

                        yield {
                            **ev, "type": "tool.call",
                            "iteration": iteration,
                            "tool_name": tb.tool_name,
                            "tool_call_id": tb.tool_id,
                            "tool_input": parsed_input,
                        }

                        # Check cancellation
                        run_state = self._active_runs.get(task.task_id)
                        if run_state and run_state.cancel_requested:
                            raise asyncio.CancelledError()

                        assert self._tool_executor is not None
                        result_str = await self._tool_executor.execute(tb.tool_name, parsed_input)

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
                        "message": f"Executed {len(turn_tool_blocks)} tool(s), continuing...",
                    }
                    continue

                # ── Final response (end_turn or other) ──────────
                break

            # ── Emit completion ─────────────────────────────────
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
                "tool_iterations": iteration,
            }
            self.task_ledger.mark_completed(task.task_id, result=result_payload)
            elapsed_ms = max(1, int((time.perf_counter() - started_at) * 1000))

            yield {
                **ev,
                "type": "response.complete",
                "content": display_text,
                "route": "opus",
                "awaiting_reply": awaiting_reply,
                "thinking_text": full_reasoning_text,
                "metrics": {"rtt_ms": elapsed_ms, "tool_iterations": iteration, **cumulative_usage},
            }
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

    # ════════════════════════════════════════════════════════════
    #  Anthropic API streaming
    # ════════════════════════════════════════════════════════════

    async def _stream_anthropic_events(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[SSEEvent]:
        url = "https://api.anthropic.com/v1/messages"
        body: dict[str, Any] = {
            "model": self.config.anthropic_model,
            "max_tokens": self.config.max_tokens,
            "stream": True,
            "thinking": {"type": "adaptive"},
            "system": system_prompt,
            "messages": messages,
        }
        if tools:
            body["tools"] = tools

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
        raw_context = task.input.get("conversation_context")
        context = raw_context if isinstance(raw_context, list) else []
        normalized = normalize_conversation_history(context)
        messages: list[dict[str, Any]] = []
        for item in normalized:
            role = str(item.get("role") or "").strip()
            content = str(item.get("content") or "").strip()
            if role not in {"user", "assistant"} or not content:
                continue
            messages.append({"role": role, "content": content})

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
