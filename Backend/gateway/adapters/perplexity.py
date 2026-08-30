from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import re
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import urlparse

import httpx

from .prompts import build_direct_assistant_system_prompt
from .response_processor import DirectRouteHandoff, LLMStreamProcessor, normalize_conversation_history
from shared import begin_metered_call, build_model_key


class PerplexityAdapter(LLMStreamProcessor):
    """Direct Perplexity streaming adapter for Gateway-routed chat."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_sec: float,
    ) -> None:
        self.api_key = api_key.strip()
        self.model = model.strip()
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
            raise RuntimeError("PERPLEXITY_API_KEY is not configured on the Gateway VM.")
        if not self.model:
            raise RuntimeError("PERPLEXITY_MODEL is not configured on the Gateway VM.")

        source_task: asyncio.Task[list[dict[str, str]]] | None = None
        citations: list[str] = []
        usage: dict[str, int] = {}

        async def stream_text() -> AsyncIterator[str]:
            nonlocal source_task, citations, usage
            async for text, citation_batch, usage_batch in self._stream_events(
                history,
                memory_context=memory_context,
                usage_recorder=usage_recorder,
            ):
                usage = self._merge_usage(usage, usage_batch)
                if citation_batch and not source_task:
                    citations = citation_batch
                    source_task = asyncio.create_task(self._enrich_sources(citation_batch))
                if text:
                    yield text

        result = await self.process_stream(
            stream_text(),
            request_id=request_id,
            session_id=session_id,
            send=send,
        )
        if result.handoff_route is not None:
            if source_task is not None:
                source_task.cancel()
                with suppress(asyncio.CancelledError):
                    await source_task
            raise DirectRouteHandoff(result.handoff_route)

        sources = await source_task if source_task else self._normalize_sources(citations)
        metadata: dict[str, Any] = {
            "legacy_route": "perplexity",
            "dispatch_target": "research",
            "model_provider": "perplexity",
            "model": self.model,
            "preferred_model_provider": "perplexity",
            "preferred_model": self.model,
        }
        if sources:
            metadata["sources"] = sources
        complete_payload = {
            "type": "response.complete",
            "request_id": request_id,
            "session_id": session_id,
            "content": result.content,
            "route": "perplexity",
            "legacy_route": "perplexity",
            "dispatch_target": "research",
            "model_provider": "perplexity",
            "model": self.model,
            "preferred_model_provider": "perplexity",
            "preferred_model": self.model,
            "awaiting_reply": result.awaiting_reply,
            "metrics": {
                **result.metrics,
                **usage,
            },
        }
        if sources:
            complete_payload["sources"] = sources

        await send(complete_payload)
        store_assistant_message(
            result.content,
            awaiting_reply=result.awaiting_reply,
            metadata=metadata,
            channel=channel,
            route="perplexity",
        )

    async def _stream_events(
        self,
        history: list[dict[str, Any]],
        *,
        memory_context: str | None = None,
        usage_recorder: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> AsyncIterator[tuple[str, list[str], dict[str, int]]]:
        url = "https://api.perplexity.ai/chat/completions"
        payload = {
            "model": self.model,
            "messages": self._build_messages(history, memory_context=memory_context),
            "stream": True,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        for attempt in range(3):
            yielded_any = False
            usage: dict[str, int] = {}
            metered_call = begin_metered_call(prefix="call")
            provider_request_id: str | None = None
            try:
                async with self._client.stream("POST", url, headers=headers, json=payload) as response:
                    provider_request_id = (
                        response.headers.get("x-request-id")
                        or response.headers.get("request-id")
                        or response.headers.get("x-perplexity-request-id")
                        or None
                    )
                    if response.status_code >= 400:
                        body = await response.aread()
                        raise RuntimeError(self._error_from_response(body, response.status_code))

                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if not data_str:
                            continue
                        if data_str == "[DONE]":
                            break

                        parsed = json.loads(data_str)
                        citations = self._extract_citations(parsed)
                        text = self._extract_delta_text(parsed)
                        usage_batch = self._extract_usage(parsed)
                        usage = self._merge_usage(usage, usage_batch)
                        if text or citations or usage_batch:
                            yielded_any = yielded_any or bool(text)
                            yield text, citations, usage_batch
                await self._emit_usage(
                    usage_recorder,
                    {
                        "metered_call": metered_call,
                        "model_key": build_model_key("perplexity", self.model),
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
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                await self._emit_usage(
                    usage_recorder,
                    {
                        "metered_call": metered_call,
                        "model_key": build_model_key("perplexity", self.model),
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
                    raise RuntimeError(f"Perplexity API error: {exc}") from exc
                await asyncio.sleep(0.5 * (2**attempt))

    def _build_messages(
        self,
        history: list[dict[str, Any]],
        *,
        memory_context: str | None = None,
    ) -> list[dict[str, str]]:
        messages = [{"role": "system", "content": build_direct_assistant_system_prompt(memory_context)}]
        for message in normalize_conversation_history(history):
            role = str(message.get("role") or "").strip()
            content = str(message.get("content") or "").strip()
            if role not in {"user", "assistant"} or not content:
                continue
            messages.append({"role": role, "content": content})
        return messages

    def _extract_delta_text(self, payload: dict[str, Any]) -> str:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        delta = first.get("delta")
        if not isinstance(delta, dict):
            return ""
        return str(delta.get("content") or "")

    def _extract_citations(self, payload: dict[str, Any]) -> list[str]:
        raw = payload.get("citations")
        if not isinstance(raw, list):
            return []
        seen: list[str] = []
        for item in raw:
            url = str(item or "").strip()
            if url and url not in seen:
                seen.append(url)
        return seen

    async def _enrich_sources(self, urls: list[str]) -> list[dict[str, str]]:
        tasks = [self._enrich_source(url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        sources: list[dict[str, str]] = []
        for index, item in enumerate(results):
            if isinstance(item, dict):
                sources.append(item)
                continue
            fallback = self._normalize_sources([urls[index]])
            if fallback:
                sources.append(fallback[0])
        return sources

    async def _enrich_source(self, url: str) -> dict[str, str]:
        parsed = urlparse(url)
        domain = parsed.netloc.replace("www.", "")
        source = {
            "url": url,
            "domain": domain,
            "title": domain or url,
        }
        try:
            response = await self._client.get(
                url,
                headers={"User-Agent": "CosmicGateway/1.0"},
                timeout=httpx.Timeout(1.5, connect=1.0),
                follow_redirects=True,
            )
            if response.status_code >= 400:
                return source
            match = re.search(r"<title>(.*?)</title>", response.text, flags=re.IGNORECASE | re.DOTALL)
            if match:
                title = " ".join(match.group(1).split())
                if title:
                    source["title"] = title
        except Exception:
            return source
        return source

    def _extract_usage(self, payload: dict[str, Any]) -> dict[str, int]:
        usage = payload.get("usage")
        if isinstance(usage, dict):
            return self._merge_usage({}, usage)
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                return self._merge_usage({}, first.get("usage"))
        return {}

    def _merge_usage(self, existing: dict[str, int], usage: Any) -> dict[str, int]:
        if not isinstance(usage, dict):
            return existing
        merged = dict(existing)
        for key, value in usage.items():
            if isinstance(value, (int, float)):
                merged[key] = int(value)
        return merged

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

    def _normalize_sources(self, urls: list[str]) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        for url in urls:
            parsed = urlparse(url)
            domain = parsed.netloc.replace("www.", "")
            normalized.append(
                {
                    "url": url,
                    "domain": domain,
                    "title": domain or url,
                }
            )
        return normalized

    def _error_from_response(self, body: bytes, status_code: int) -> str:
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return f"status={status_code}"

        if isinstance(payload, dict):
            error = str(payload.get("error") or "").strip()
            if error:
                return error
            message = str(payload.get("message") or "").strip()
            if message:
                return message
        return f"status={status_code}"
