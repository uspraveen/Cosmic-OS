from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

import httpx

from .prompts import DIRECT_ASSISTANT_SYSTEM_PROMPT
from .response_processor import LLMStreamProcessor


class GeminiAdapter(LLMStreamProcessor):
    """Direct Gemini streaming adapter for Gateway-routed chat."""

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
    ) -> None:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured on the Gateway VM.")
        if not self.model:
            raise RuntimeError("GEMINI_MODEL is not configured on the Gateway VM.")

        result = await self.process_stream(
            self._stream_text(history),
            request_id=request_id,
            session_id=session_id,
            send=send,
        )

        await send(
            {
                "type": "response.complete",
                "request_id": request_id,
                "session_id": session_id,
                "content": result.content,
                "route": "gemini",
                "awaiting_reply": result.awaiting_reply,
                "metrics": result.metrics,
            }
        )
        store_assistant_message(
            result.content,
            awaiting_reply=result.awaiting_reply,
            metadata=None,
            channel=channel,
            route="gemini",
        )

    async def _stream_text(self, history: list[dict[str, Any]]) -> AsyncIterator[str]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:streamGenerateContent?alt=sse"
        payload = {
            "system_instruction": {
                "parts": [{"text": DIRECT_ASSISTANT_SYSTEM_PROMPT}],
            },
            "contents": self._build_contents(history),
        }
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }

        for attempt in range(3):
            accumulated = ""
            yielded_any = False
            try:
                async with self._client.stream("POST", url, headers=headers, json=payload) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        raise RuntimeError(self._error_from_response(body, response.status_code))

                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if not data_str or data_str == "[DONE]":
                            continue

                        parsed = json.loads(data_str)
                        text = self._extract_text(parsed)
                        if not text:
                            continue

                        if text.startswith(accumulated):
                            delta = text[len(accumulated) :]
                            accumulated = text
                        else:
                            delta = text
                            accumulated += text

                        if delta:
                            yielded_any = True
                            yield delta
                return
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                if yielded_any or attempt == 2:
                    raise RuntimeError(f"Gemini API error: {exc}") from exc
                await self._backoff(attempt)

    async def _backoff(self, attempt: int) -> None:
        delay = 0.5 * (2**attempt)
        if delay:
            await asyncio.sleep(delay)

    def _build_contents(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        contents: list[dict[str, Any]] = []
        for message in history:
            role = "model" if message.get("role") == "assistant" else "user"
            content = str(message.get("content") or "").strip()
            if not content:
                continue
            contents.append(
                {
                    "role": role,
                    "parts": [{"text": content}],
                }
            )
        return contents

    def _extract_text(self, payload: dict[str, Any]) -> str:
        texts: list[str] = []
        for candidate in payload.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content")
            if not isinstance(content, dict):
                continue
            for part in content.get("parts") or []:
                if not isinstance(part, dict):
                    continue
                text = str(part.get("text") or "")
                if text:
                    texts.append(text)
        return "".join(texts)

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
