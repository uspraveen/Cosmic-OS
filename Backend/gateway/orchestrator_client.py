from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from shared import TaskEnvelope


class OrchestratorClient:
    """Internal Gateway client for the thin Opus orchestrator service."""

    def __init__(
        self,
        *,
        base_url: str,
        internal_token: str,
        timeout_sec: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.internal_token = internal_token.strip()
        timeout = httpx.Timeout(timeout_sec, connect=min(timeout_sec, 10.0))
        self._client = httpx.AsyncClient(timeout=timeout, http2=True)

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        await self._client.aclose()

    async def stream_task(self, task: TaskEnvelope) -> AsyncIterator[dict[str, Any]]:
        headers = {
            "Content-Type": "application/json",
            "X-Internal-Token": self.internal_token,
        }
        url = f"{self.base_url}/internal/process/stream"
        async with self._client.stream(
            "POST",
            url,
            headers=headers,
            json=task.model_dump(mode="json"),
        ) as response:
            if response.status_code >= 400:
                body = await response.aread()
                raise RuntimeError(self._error_from_response(body, response.status_code))

            async for line in response.aiter_lines():
                payload = line.strip()
                if not payload:
                    continue
                parsed = json.loads(payload)
                if isinstance(parsed, dict):
                    yield parsed

    async def list_active_tasks(
        self,
        *,
        session_id: str | None = None,
        channel: str | None = None,
    ) -> list[dict[str, Any]]:
        headers = {
            "X-Internal-Token": self.internal_token,
        }
        params: dict[str, str] = {}
        if session_id:
            params["session_id"] = session_id
        if channel:
            params["channel"] = channel
        response = await self._client.get(
            f"{self.base_url}/internal/tasks/active",
            headers=headers,
            params=params,
        )
        if response.status_code >= 400:
            raise RuntimeError(self._error_from_response(response.content, response.status_code))
        payload = response.json()
        tasks = payload.get("tasks")
        if not isinstance(tasks, list):
            return []
        return [item for item in tasks if isinstance(item, dict)]

    async def cancel_task(self, task_id: str) -> bool:
        normalized_task_id = str(task_id or "").strip()
        if not normalized_task_id:
            return False
        headers = {
            "X-Internal-Token": self.internal_token,
        }
        response = await self._client.post(
            f"{self.base_url}/internal/tasks/{normalized_task_id}/cancel",
            headers=headers,
        )
        if response.status_code >= 400:
            raise RuntimeError(self._error_from_response(response.content, response.status_code))
        payload = response.json()
        return bool(payload.get("cancelled"))

    async def health(self, *, timeout_sec: float | None = None) -> dict[str, Any]:
        headers = {
            "X-Internal-Token": self.internal_token,
        }
        response = await self._client.get(
            f"{self.base_url}/health",
            headers=headers,
            timeout=timeout_sec,
        )
        if response.status_code >= 400:
            raise RuntimeError(self._error_from_response(response.content, response.status_code))
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Orchestrator health returned a non-object response")
        return payload

    async def list_registry_agents(self) -> dict[str, Any]:
        headers = {
            "X-Internal-Token": self.internal_token,
        }
        response = await self._client.get(
            f"{self.base_url}/internal/registry/agents",
            headers=headers,
        )
        if response.status_code >= 400:
            raise RuntimeError(self._error_from_response(response.content, response.status_code))
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Orchestrator registry agents returned a non-object response")
        return payload

    def _error_from_response(self, body: bytes, status_code: int) -> str:
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return f"status={status_code}"

        if isinstance(payload, dict):
            detail = payload.get("detail")
            if isinstance(detail, str) and detail.strip():
                return detail.strip()
            error = payload.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or "").strip()
                if message:
                    return message
        return f"status={status_code}"
