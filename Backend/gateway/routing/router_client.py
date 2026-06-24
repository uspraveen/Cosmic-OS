from __future__ import annotations

import time
from typing import Any

import httpx


class ModelRouterClient:
    """Small async client for the internal model-router service."""

    def __init__(self, base_url: str, timeout_sec: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = timeout_sec
        self._client: httpx.AsyncClient | None = None
        self._disabled_until_monotonic = 0.0
        self._last_disable_reason = ""

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout_sec, connect=min(self.timeout_sec, 5.0)),
            )
            try:
                await self.health(timeout_sec=min(self.timeout_sec, 2.0))
            except Exception:
                # Startup must not fail because the optional router is degraded.
                pass

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

        now = time.monotonic()
        if self._disabled_until_monotonic > now:
            remaining_ms = int((self._disabled_until_monotonic - now) * 1000)
            reason = self._last_disable_reason or "recent router failure"
            raise RuntimeError(
                f"Model router circuit open for {remaining_ms}ms after {reason}"
            )

        try:
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
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            if status_code in {401, 403}:
                self._open_circuit(
                    seconds=300.0,
                    reason=f"provider auth HTTP {status_code}",
                )
            elif status_code >= 500:
                self._open_circuit(
                    seconds=20.0,
                    reason=f"router/provider HTTP {status_code}",
                )
            raise
        except httpx.RequestError as exc:
            self._open_circuit(seconds=20.0, reason=exc.__class__.__name__)
            raise

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

        response = await self._client.get("/health/ready", timeout=timeout_sec)
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Model router health returned a non-object response")
        if response.status_code >= 400:
            payload = {
                **payload,
                "status": payload.get("status") or "not_ready",
                "http_status": response.status_code,
            }
            provider_status = payload.get("last_provider_status_code")
            if provider_status in {401, 403}:
                self._open_circuit(
                    seconds=300.0,
                    reason=f"provider auth HTTP {provider_status}",
                )
        if self._disabled_until_monotonic > time.monotonic():
            payload = {
                **payload,
                "status": "not_ready",
                "circuit_open": True,
                "circuit_reason": self._last_disable_reason,
                "circuit_remaining_ms": int(
                    (self._disabled_until_monotonic - time.monotonic()) * 1000
                ),
            }
        return payload

    def _open_circuit(self, *, seconds: float, reason: str) -> None:
        self._disabled_until_monotonic = max(
            self._disabled_until_monotonic,
            time.monotonic() + max(0.0, seconds),
        )
        self._last_disable_reason = reason
