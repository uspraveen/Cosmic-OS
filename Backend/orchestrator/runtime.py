from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from gateway.adapters.response_processor import AWAITING_REPLY_TAG
from gateway.adapters.response_processor import normalize_conversation_history
from shared import TaskEnvelope, verify_task_envelope

from .config import OrchestratorConfig
from .ledger import TaskLedger
from .prompts import THIN_ORCHESTRATOR_SYSTEM_PROMPT


@dataclass(slots=True)
class SSEEvent:
    event: str
    data: str


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
    ) -> None:
        self.config = config
        timeout = httpx.Timeout(config.request_timeout_sec, connect=min(config.request_timeout_sec, 15.0))
        self._client = client or httpx.AsyncClient(timeout=timeout, http2=True)
        self._owns_client = client is None
        self.task_ledger = TaskLedger(config.task_ledger_db_path)
        self.started = False
        self._active_runs: dict[str, ActiveTaskRun] = {}

    async def start(self) -> None:
        self.task_ledger.initialize()
        self.started = True

    async def stop(self) -> None:
        if self._owns_client:
            await self._client.aclose()
        self.started = False

    async def stream_task(self, task: TaskEnvelope) -> AsyncIterator[dict[str, Any]]:
        if not verify_task_envelope(task, self.config.signing_secret):
            raise RuntimeError("TaskEnvelope signature verification failed.")
        if not self.config.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured in orchestrator.env.")
        if not self.config.anthropic_model:
            raise RuntimeError("ANTHROPIC_MODEL is not configured in orchestrator.env.")

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
        yield {
            "type": "task.created",
            "task_id": task.task_id,
            "request_id": request_id,
            "session_id": session_id,
            "channel": channel,
            "route": "opus",
            "status": "running",
        }

        started_at = time.perf_counter()
        reasoning_started = False
        response_started = False
        full_reasoning = ""
        full_response = ""
        usage: dict[str, Any] = {}
        stop_reason: str | None = None

        try:
            async for event in self._stream_anthropic_events(task):
                if event.event == "ping":
                    continue
                if not event.data:
                    continue

                payload = json.loads(event.data)
                payload_type = str(payload.get("type") or "").strip()
                if payload_type == "message_start":
                    usage = self._merge_usage(usage, payload.get("message", {}).get("usage"))
                    continue

                if payload_type == "message_delta":
                    usage = self._merge_usage(usage, payload.get("usage"))
                    delta = payload.get("delta")
                    if isinstance(delta, dict):
                        stop_reason = str(delta.get("stop_reason") or "").strip() or stop_reason
                    continue

                if payload_type == "error":
                    error = payload.get("error")
                    if isinstance(error, dict):
                        message = str(error.get("message") or "").strip() or "Anthropic stream error"
                    else:
                        message = "Anthropic stream error"
                    raise RuntimeError(message)

                if payload_type != "content_block_delta":
                    continue

                delta = payload.get("delta")
                if not isinstance(delta, dict):
                    continue
                delta_type = str(delta.get("type") or "").strip()

                if delta_type == "thinking_delta":
                    thinking = str(delta.get("thinking") or "")
                    if not thinking:
                        continue
                    if not reasoning_started:
                        reasoning_started = True
                        yield {
                            "type": "task.progress",
                            "task_id": task.task_id,
                            "request_id": request_id,
                            "session_id": session_id,
                            "channel": channel,
                            "status": "thinking",
                            "message": "Opus is reasoning through the request.",
                        }
                    full_reasoning += thinking
                    yield {
                        "type": "response.thinking.chunk",
                        "task_id": task.task_id,
                        "request_id": request_id,
                        "session_id": session_id,
                        "channel": channel,
                        "content": thinking,
                        "done": False,
                    }
                    continue

                if delta_type != "text_delta":
                    continue
                text = str(delta.get("text") or "")
                if not text:
                    continue
                if not response_started:
                    response_started = True
                    yield {
                        "type": "task.progress",
                        "task_id": task.task_id,
                        "request_id": request_id,
                        "session_id": session_id,
                        "channel": channel,
                        "status": "responding",
                        "message": "Opus is writing the response.",
                    }
                full_response += text
                yield {
                    "type": "response.chunk",
                    "task_id": task.task_id,
                    "request_id": request_id,
                    "session_id": session_id,
                    "channel": channel,
                    "content": text,
                    "done": False,
                }

            display_text = full_response.rstrip()
            awaiting_reply = display_text.endswith(AWAITING_REPLY_TAG)
            if awaiting_reply:
                display_text = display_text.removesuffix(AWAITING_REPLY_TAG).rstrip()

            result_payload = {
                "content": display_text,
                "thinking_text": full_reasoning,
                "awaiting_reply": awaiting_reply,
                "usage": usage,
                "stop_reason": stop_reason,
            }
            self.task_ledger.mark_completed(task.task_id, result=result_payload)
            elapsed_ms = max(1, int((time.perf_counter() - started_at) * 1000))
            yield {
                "type": "response.complete",
                "task_id": task.task_id,
                "request_id": request_id,
                "session_id": session_id,
                "channel": channel,
                "content": display_text,
                "route": "opus",
                "awaiting_reply": awaiting_reply,
                "thinking_text": full_reasoning,
                "metrics": {
                    "rtt_ms": elapsed_ms,
                    **usage,
                },
            }
            yield {
                "type": "task.completed",
                "task_id": task.task_id,
                "request_id": request_id,
                "session_id": session_id,
                "channel": channel,
                "route": "opus",
                "status": "completed",
            }
        except asyncio.CancelledError:
            run_state = self._active_runs.get(task.task_id)
            if run_state and run_state.cancel_requested:
                message = run_state.cancel_message
                self.task_ledger.mark_cancelled(task.task_id, message=message)
                yield {
                    "type": "task.cancelled",
                    "task_id": task.task_id,
                    "request_id": request_id,
                    "session_id": session_id,
                    "channel": channel,
                    "route": "opus",
                    "status": "cancelled",
                    "message": message,
                }
                return
            raise
        except Exception as exc:
            message = str(exc).strip() or "Orchestrator processing failed."
            self.task_ledger.mark_failed(task.task_id, code="OPUS_UPSTREAM_ERROR", message=message)
            yield {
                "type": "task.failed",
                "task_id": task.task_id,
                "request_id": request_id,
                "session_id": session_id,
                "channel": channel,
                "route": "opus",
                "status": "failed",
                "error": {
                    "code": "OPUS_UPSTREAM_ERROR",
                    "message": message,
                    "retryable": False,
                },
            }
        finally:
            self._active_runs.pop(task.task_id, None)

    def list_active_tasks(
        self,
        *,
        session_id: str | None = None,
        channel: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.task_ledger.list_active_tasks(session_id=session_id, channel=channel)

    def cancel_task(self, task_id: str, *, message: str = "Response stopped.") -> bool:
        normalized_task_id = str(task_id or "").strip()
        if not normalized_task_id:
            return False
        run_state = self._active_runs.get(normalized_task_id)
        if run_state is None:
            return False
        run_state.cancel_requested = True
        run_state.cancel_message = message
        runner_task = run_state.runner_task
        if runner_task is not None and not runner_task.done():
            runner_task.cancel()
        return True

    async def _stream_anthropic_events(self, task: TaskEnvelope) -> AsyncIterator[SSEEvent]:
        url = "https://api.anthropic.com/v1/messages"
        payload = {
            "model": self.config.anthropic_model,
            "max_tokens": self.config.max_tokens,
            "stream": True,
            "thinking": {"type": "adaptive"},
            "system": THIN_ORCHESTRATOR_SYSTEM_PROMPT,
            "messages": self._build_messages(task),
        }
        headers = {
            "x-api-key": self.config.anthropic_api_key,
            "anthropic-version": self.config.anthropic_version,
            "content-type": "application/json",
            "accept": "text/event-stream",
        }

        for attempt in range(3):
            yielded_any = False
            try:
                async with self._client.stream("POST", url, headers=headers, json=payload) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        raise RuntimeError(self._error_from_response(body, response.status_code))

                    async for item in self._iter_sse(response):
                        yielded_any = True
                        yield item
                return
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                if yielded_any or attempt == 2:
                    raise RuntimeError(f"Anthropic API error: {exc}") from exc
                await asyncio.sleep(0.5 * (2**attempt))

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
                "",
                "Attachment manifest:",
            ]
            for index, artifact in enumerate(input_artifacts, start=1):
                if not isinstance(artifact, dict):
                    continue
                summary_parts = [
                    f"kind={str(artifact.get('kind') or 'unknown').strip()}",
                    f"mime={str(artifact.get('mime') or 'application/octet-stream').strip()}",
                ]
                filename = str(artifact.get("filename") or "").strip()
                caption = str(artifact.get("caption") or "").strip()
                bridge_media_ref = str(artifact.get("bridge_media_ref") or "").strip()
                download_url = str(artifact.get("download_url") or "").strip()
                size_bytes = artifact.get("size_bytes")
                if filename:
                    summary_parts.append(f"filename={filename}")
                if caption:
                    summary_parts.append(f"caption={caption}")
                if size_bytes:
                    summary_parts.append(f"size_bytes={size_bytes}")
                if bridge_media_ref:
                    summary_parts.append(f"bridge_media_ref={bridge_media_ref}")
                if download_url:
                    summary_parts.append(f"download_url={download_url}")
                manifest_lines.append(f"{index}. " + "; ".join(summary_parts))
            user_query = user_query + "\n\n" + "\n".join(manifest_lines) if user_query else "\n".join(manifest_lines)
        messages.append({"role": "user", "content": user_query})
        return messages

    def _merge_usage(self, existing: dict[str, Any], usage: Any) -> dict[str, Any]:
        if not isinstance(usage, dict):
            return existing
        merged = dict(existing)
        for key, value in usage.items():
            if isinstance(value, (int, float)):
                merged[key] = int(value)
        return merged

    def _error_from_response(self, body: bytes, status_code: int) -> str:
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return f"status={status_code}"

        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or "").strip()
                if message:
                    return message
        return f"status={status_code}"
