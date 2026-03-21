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
        memory_context: str | None = None,
        max_completion_tokens: int = 430,
    ) -> dict[str, Any]:
        payload = await self.classify_with_metadata(
            query=query,
            conversation_context=conversation_context,
            memory_context=memory_context,
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
        memory_context: str | None = None,
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
                "memory_context": memory_context,
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
            **payload,
            "classification": classification,
        }

    async def health(self, *, timeout_sec: float | None = None) -> dict[str, Any]:
        if self._client is None:
            await self.start()

        if self._client is None:
            raise RuntimeError("Model router client is not initialized")

        response = await self._client.get("/health", timeout=timeout_sec)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Model router health returned a non-object response")
        return payload
