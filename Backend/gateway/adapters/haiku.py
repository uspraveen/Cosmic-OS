from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx

from .prompts import build_direct_assistant_system_prompt
from .response_processor import DirectRouteHandoff, LLMStreamProcessor, normalize_conversation_history
from shared import begin_metered_call, build_model_key


@dataclass(slots=True)
class SSEEvent:
    event: str
    data: str


class HaikuAdapter(LLMStreamProcessor):
    """Direct Claude Haiku 4.5 streaming adapter for Gateway-routed chat."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        anthropic_version: str,
        max_tokens: int,
        thinking_budget_tokens: int,
        timeout_sec: float,
    ) -> None:
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.anthropic_version = anthropic_version.strip() or "2023-06-01"
        self.max_tokens = max(1024, int(max_tokens))
        self.thinking_budget_tokens = max(0, int(thinking_budget_tokens))
        self.timeout = httpx.Timeout(timeout_sec, connect=min(timeout_sec, 10.0))
        self._client = httpx.AsyncClient(timeout=self.timeout, http2=True)

    async def close(self) -> None:
        await self._client.aclose()

    async def stream(
        self,
        *,
        request_id: str,
        session_id: str,
        history: list[dict[str, Any]],
        send,
        store_assistant_message,
        channel: str,
        memory_context: str | None = None,
        usage_recorder: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured on the Gateway VM.")
        if not self.model:
            raise RuntimeError("HAIKU_MODEL is not configured on the Gateway VM.")

        usage: dict[str, int] = {}
        stop_reason: str | None = None
        thinking_text = ""
        pending_thinking_chunks: list[str] = []

        async def flush_thinking_chunks() -> None:
            if not pending_thinking_chunks:
                return
            for thinking_chunk in pending_thinking_chunks:
                await send(
                    {
                        "type": "response.thinking.chunk",
                        "request_id": request_id,
                        "session_id": session_id,
                        "content": thinking_chunk,
                        "done": False,
                    }
                )
            pending_thinking_chunks.clear()

        async def text_stream() -> AsyncIterator[str]:
            nonlocal stop_reason, thinking_text, usage
            async for payload in self._stream_events(
                history,
                memory_context=memory_context,
                usage_recorder=usage_recorder,
            ):
                payload_type = str(payload.get("type") or "").strip()
                if payload_type == "usage":
                    usage = self._merge_usage(usage, payload.get("usage"))
                    continue
                if payload_type == "stop_reason":
                    candidate = str(payload.get("stop_reason") or "").strip()
                    if candidate:
                        stop_reason = candidate
                    continue
                if payload_type == "thinking":
                    chunk = str(payload.get("content") or "")
                    if not chunk:
                        continue
                    thinking_text += chunk
                    pending_thinking_chunks.append(chunk)
                    continue
                if payload_type == "text":
                    chunk = str(payload.get("content") or "")
                    if chunk:
                        yield chunk

        result = await self.process_stream(
            text_stream(),
            request_id=request_id,
            session_id=session_id,
            send=send,
            on_first_visible_chunk=flush_thinking_chunks,
        )
        if result.handoff_route is not None:
            raise DirectRouteHandoff(result.handoff_route)

        metadata: dict[str, Any] = {
            "legacy_route": "haiku",
            "dispatch_target": "direct",
            "model_provider": "anthropic",
            "model": self.model,
            "preferred_model_provider": "anthropic",
            "preferred_model": self.model,
        }
        if thinking_text or stop_reason:
            if thinking_text:
                metadata["thinking_text"] = thinking_text
            if stop_reason:
                metadata["stop_reason"] = stop_reason

        await send(
            {
                "type": "response.complete",
                "request_id": request_id,
                "session_id": session_id,
                "content": result.content,
                "route": "haiku",
                "legacy_route": "haiku",
                "dispatch_target": "direct",
                "model_provider": "anthropic",
                "model": self.model,
                "preferred_model_provider": "anthropic",
                "preferred_model": self.model,
                "awaiting_reply": result.awaiting_reply,
                "thinking_text": thinking_text,
                "metrics": {
                    **result.metrics,
                    **usage,
                },
            }
        )
        store_assistant_message(
            result.content,
            awaiting_reply=result.awaiting_reply,
            metadata=metadata,
            channel=channel,
            route="haiku",
        )

    async def generate_text(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        usage_recorder: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> tuple[str, dict[str, int], str | None]:
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured on the Gateway VM.")
        if not self.model:
            raise RuntimeError("HAIKU_MODEL is not configured on the Gateway VM.")

        url = "https://api.anthropic.com/v1/messages"
        payload = {
            "model": self.model,
            "max_tokens": max(256, int(max_tokens)),
            "system": system_prompt,
            "messages": messages,
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.anthropic_version,
            "content-type": "application/json",
        }

        metered_call = begin_metered_call(prefix="call")
        response = await self._client.post(url, headers=headers, json=payload)
        provider_request_id = (
            response.headers.get("request-id")
            or response.headers.get("x-request-id")
            or response.headers.get("anthropic-request-id")
            or None
        )
        if response.status_code >= 400:
            await self._emit_usage(
                usage_recorder,
                {
                    "metered_call": metered_call,
                    "model_key": build_model_key("anthropic", self.model),
                    "provider_request_id": provider_request_id,
                    "raw_usage": None,
                    "success": False,
                    "error_code": f"HTTP_{response.status_code}",
                    "metadata_json": {
                        "status_code": response.status_code,
                        "operation": "generate_text",
                    },
                },
            )
            raise RuntimeError(self._error_from_response(response.content, response.status_code))

        body = response.json()
        if not isinstance(body, dict):
            await self._emit_usage(
                usage_recorder,
                {
                    "metered_call": metered_call,
                    "model_key": build_model_key("anthropic", self.model),
                    "provider_request_id": provider_request_id,
                    "raw_usage": None,
                    "success": False,
                    "error_code": "INVALID_RESPONSE",
                    "metadata_json": {
                        "operation": "generate_text",
                    },
                },
            )
            raise RuntimeError("Anthropic Haiku API returned a non-object response")

        content_blocks = body.get("content")
        text_parts: list[str] = []
        if isinstance(content_blocks, list):
            for block in content_blocks:
                if not isinstance(block, dict):
                    continue
                if str(block.get("type") or "").strip() != "text":
                    continue
                text = str(block.get("text") or "")
                if text:
                    text_parts.append(text)

        usage = self._merge_usage({}, body.get("usage"))
        stop_reason = str(body.get("stop_reason") or "").strip() or None
        await self._emit_usage(
            usage_recorder,
            {
                "metered_call": metered_call,
                "model_key": build_model_key("anthropic", self.model),
                "provider_request_id": provider_request_id,
                "raw_usage": usage,
                "success": True,
                "metadata_json": {
                    "operation": "generate_text",
                    "stop_reason": stop_reason,
                },
            },
        )
        return "".join(text_parts).strip(), usage, stop_reason

    async def _stream_events(
        self,
        history: list[dict[str, Any]],
        *,
        memory_context: str | None = None,
        usage_recorder: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        url = "https://api.anthropic.com/v1/messages"
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "stream": True,
            "system": build_direct_assistant_system_prompt(memory_context),
            "messages": self._build_messages(history),
        }
        if self.thinking_budget_tokens > 0:
            payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.thinking_budget_tokens,
            }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.anthropic_version,
            "content-type": "application/json",
            "accept": "text/event-stream",
        }

        for attempt in range(3):
            yielded_any = False
            usage: dict[str, int] = {}
            metered_call = begin_metered_call(prefix="call")
            provider_request_id: str | None = None
            try:
                async with self._client.stream("POST", url, headers=headers, json=payload) as response:
                    provider_request_id = (
                        response.headers.get("request-id")
                        or response.headers.get("x-request-id")
                        or response.headers.get("anthropic-request-id")
                        or None
                    )
                    if response.status_code >= 400:
                        body = await response.aread()
                        raise RuntimeError(self._error_from_response(body, response.status_code))

                    async for item in self._iter_sse(response):
                        if item.event == "ping":
                            continue
                        if not item.data:
                            continue

                        yielded_any = True
                        parsed = json.loads(item.data)
                        payload_type = str(parsed.get("type") or "").strip()

                        if payload_type == "message_start":
                            message = parsed.get("message")
                            if isinstance(message, dict):
                                usage = self._merge_usage(usage, message.get("usage"))
                                yield {"type": "usage", "usage": message.get("usage")}
                            continue

                        if payload_type == "message_delta":
                            usage = self._merge_usage(usage, parsed.get("usage"))
                            yield {"type": "usage", "usage": parsed.get("usage")}
                            delta = parsed.get("delta")
                            if isinstance(delta, dict):
                                stop_reason = str(delta.get("stop_reason") or "").strip()
                                if stop_reason:
                                    yield {"type": "stop_reason", "stop_reason": stop_reason}
                            continue

                        if payload_type == "error":
                            error = parsed.get("error")
                            if isinstance(error, dict):
                                message = str(error.get("message") or "").strip() or "Anthropic stream error"
                            else:
                                message = "Anthropic stream error"
                            raise RuntimeError(message)

                        if payload_type != "content_block_delta":
                            continue

                        delta = parsed.get("delta")
                        if not isinstance(delta, dict):
                            continue

                        delta_type = str(delta.get("type") or "").strip()
                        if delta_type == "thinking_delta":
                            yield {"type": "thinking", "content": str(delta.get("thinking") or "")}
                            continue
                        if delta_type == "text_delta":
                            yield {"type": "text", "content": str(delta.get("text") or "")}
                            continue
                await self._emit_usage(
                    usage_recorder,
                    {
                        "metered_call": metered_call,
                        "model_key": build_model_key("anthropic", self.model),
                        "provider_request_id": provider_request_id,
                        "raw_usage": usage,
                        "success": True,
                        "metadata_json": {
                            "attempt": attempt + 1,
                            "streaming": True,
                            "yielded_any": yielded_any,
                        },
                    },
                )
                return
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                await self._emit_usage(
                    usage_recorder,
                    {
                        "metered_call": metered_call,
                        "model_key": build_model_key("anthropic", self.model),
                        "provider_request_id": provider_request_id,
                        "raw_usage": usage,
                        "success": False,
                        "error_code": type(exc).__name__,
                        "metadata_json": {
                            "attempt": attempt + 1,
                            "streaming": True,
                            "yielded_any": yielded_any,
                        },
                    },
                )
                if yielded_any or attempt == 2:
                    raise RuntimeError(f"Anthropic Haiku API error: {exc}") from exc
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

    def _build_messages(self, history: list[dict[str, Any]]) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        for message in normalize_conversation_history(history):
            role = str(message.get("role") or "").strip()
            content = str(message.get("content") or "").strip()
            if role not in {"user", "assistant"} or not content:
                continue
            messages.append({"role": role, "content": content})
        return messages

    def _merge_usage(self, existing: dict[str, int], usage: Any) -> dict[str, int]:
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

    async def _emit_usage(
        self,
        usage_recorder: Callable[[dict[str, Any]], Awaitable[None]] | None,
        payload: dict[str, Any],
    ) -> None:
        if usage_recorder is None:
            return
        try:
            await usage_recorder(payload)
        except Exception:
            return
