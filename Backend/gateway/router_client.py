from __future__ import annotations

from typing import Any

import httpx


class ModelRouterClient:
    """Small async client for the internal model-router service."""

    def __init__(self, base_url: str, timeout_sec: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = timeout_sec
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout_sec, connect=min(self.timeout_sec, 5.0)),
            )

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def classify(
        self,
        *,
        query: str,
        conversation_context: list[dict[str, str]],
        max_completion_tokens: int = 430,
    ) -> dict[str, Any]:
        payload = await self.classify_with_metadata(
            query=query,
            conversation_context=conversation_context,
            max_completion_tokens=max_completion_tokens,
        )
        classification = payload.get("classification")
        if not isinstance(classification, dict):
            raise RuntimeError("Model router returned an invalid classification payload")
        return classification

    async def classify_with_metadata(
        self,
        *,
        query: str,
        conversation_context: list[dict[str, str]],
        max_completion_tokens: int = 430,
    ) -> dict[str, Any]:
        if self._client is None:
            await self.start()

        if self._client is None:
            raise RuntimeError("Model router client is not initialized")

        response = await self._client.post(
            "/classify",
            json={
                "query": query,
                "conversation_context": conversation_context,
                "max_completion_tokens": max_completion_tokens,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Model router returned a non-object response")
        classification = payload.get("classification", payload)
        if not isinstance(classification, dict):
            raise RuntimeError("Model router returned an invalid classification payload")
        return {
            "classification": classification,
            "metrics": payload.get("metrics") if isinstance(payload.get("metrics"), dict) else None,
            "classifier_model": payload.get("classifier_model"),
            "raw_classifier_output": payload.get("raw_classifier_output"),
            "http2_enabled": payload.get("http2_enabled"),
        }
