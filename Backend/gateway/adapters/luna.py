from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

import httpx

from shared import begin_metered_call, build_model_key, normalized_reasoning_effort


class LunaAdapter:
    """OpenAI chat-completions adapter for Gateway summarization work.

    Rides the same OpenAI endpoint the visual sidecar uses (GPT-5.6 Luna by
    default). Exists because the Anthropic credits backing HaikuAdapter have
    been exhausted for months, which silently killed every rollover summary
    and mid-day compaction. Only the non-streaming generate_text path is
    implemented; this adapter is for background summaries, not chat.
    """

    usage_source_id = "gateway:luna"
    usage_route = "luna"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        reasoning_effort: str = "low",
        timeout_sec: float = 180.0,
    ) -> None:
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.base_url = base_url.strip().rstrip("/") or "https://api.openai.com/v1"
        self.reasoning_effort = reasoning_effort.strip() or "low"
        self.timeout = httpx.Timeout(timeout_sec, connect=min(timeout_sec, 15.0))
        self._client = httpx.AsyncClient(timeout=self.timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def generate_text(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        usage_recorder: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> tuple[str, dict[str, int], str | None]:
        if not self.api_key:
            raise RuntimeError("LUNA_API_KEY is not configured on the Gateway VM.")
        if not self.model:
            raise RuntimeError("LUNA_MODEL is not configured on the Gateway VM.")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
            "max_completion_tokens": max(256, int(max_tokens)),
        }
        effort = normalized_reasoning_effort(self.model, self.reasoning_effort, default="low")
        if effort:
            payload["reasoning_effort"] = effort

        metered_call = begin_metered_call(prefix="call")
        response = await self._client.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        provider_request_id = (
            response.headers.get("x-request-id")
            or response.headers.get("request-id")
            or response.headers.get("openai-request-id")
            or None
        )
        if response.status_code >= 400:
            await self._emit_usage(
                usage_recorder,
                {
                    "metered_call": metered_call,
                    "model_key": build_model_key("openai", self.model),
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
                    "model_key": build_model_key("openai", self.model),
                    "provider_request_id": provider_request_id,
                    "raw_usage": None,
                    "success": False,
                    "error_code": "INVALID_RESPONSE",
                    "metadata_json": {
                        "operation": "generate_text",
                    },
                },
            )
            raise RuntimeError("OpenAI Luna API returned a non-object response")

        choices = body.get("choices")
        text_parts: list[str] = []
        stop_reason: str | None = None
        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            message = first.get("message") if isinstance(first.get("message"), dict) else {}
            content = str(message.get("content") or "")
            if content:
                text_parts.append(content)
            stop_reason = str(first.get("finish_reason") or "").strip() or None

        raw_usage = body.get("usage")
        usage: dict[str, int] = {}
        if isinstance(raw_usage, dict):
            for key, value in raw_usage.items():
                if isinstance(value, (int, float)):
                    usage[key] = int(value)

        await self._emit_usage(
            usage_recorder,
            {
                "metered_call": metered_call,
                "model_key": build_model_key("openai", self.model),
                "provider_request_id": provider_request_id,
                "raw_usage": raw_usage,
                "success": True,
                "metadata_json": {
                    "operation": "generate_text",
                    "stop_reason": stop_reason,
                },
            },
        )
        return "".join(text_parts).strip(), usage, stop_reason

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
