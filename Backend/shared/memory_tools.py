from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx


class GatewayToolError(RuntimeError):
    def __init__(self, *, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class _GatewayInternalClient:
    def __init__(
        self,
        *,
        gateway_url: str,
        service_token: str,
        agent_id: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.service_token = service_token.strip()
        self.agent_id = agent_id
        timeout = httpx.Timeout(30.0, connect=10.0)
        self._client = client or httpx.AsyncClient(timeout=timeout, http2=True)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> dict[str, Any] | None:
        if not self.gateway_url:
            raise RuntimeError("Gateway internal API is not configured.")

        response = await self._client.request(
            method,
            f"{self.gateway_url}{path}",
            json=json_body,
            params=params,
            headers=self._headers(),
        )
        if allow_404 and response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise GatewayToolError(
                status_code=response.status_code,
                message=self._error_from_response(response),
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Gateway returned a non-object payload.")
        return payload

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.service_token:
            headers["X-Internal-Token"] = self.service_token
        return headers

    def _error_from_response(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return response.text.strip() or f"status={response.status_code}"
        if isinstance(payload, dict):
            detail = payload.get("detail")
            if isinstance(detail, str) and detail.strip():
                return detail.strip()
            error = payload.get("error")
            if isinstance(error, str) and error.strip():
                return error.strip()
        return f"status={response.status_code}"


class MemoryRead(_GatewayInternalClient):
    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        memory_types: list[str] | None = None,
        seed_memory_ids: list[str] | None = None,
        seed_entities: list[str] | None = None,
        max_hops: int | None = None,
        include_diagnostics: bool = False,
    ) -> dict[str, Any]:
        use_active_search = bool(seed_memory_ids or seed_entities or max_hops is not None or include_diagnostics)
        payload: dict[str, Any] = {
            "query": str(query or "").strip(),
            "max_results": max(1, min(int(max_results), 20)),
            "agent_id": self.agent_id,
        }
        if memory_types:
            payload["kinds"] = [item.strip() for item in memory_types if str(item or "").strip()]
        if seed_memory_ids:
            payload["seed_memory_ids"] = [item.strip() for item in seed_memory_ids if str(item or "").strip()]
        if seed_entities:
            payload["seed_entities"] = [item.strip() for item in seed_entities if str(item or "").strip()]
        if max_hops is not None:
            payload["max_hops"] = max(1, min(int(max_hops), 6))
        if include_diagnostics:
            payload["include_diagnostics"] = True
        path = "/internal/memory/active-search" if use_active_search else "/internal/memory/search"
        return await self._request_json("POST", path, json_body=payload) or {}

    async def fetch(self, memory_id: str) -> dict[str, Any]:
        normalized = str(memory_id or "").strip()
        if not normalized:
            raise ValueError("memory_id is required")
        payload = await self._request_json(
            "GET",
            f"/internal/memory/memories/{quote(normalized, safe='')}",
            allow_404=True,
        )
        if payload is None:
            return {"found": False, "memory_id": normalized}
        return payload

    async def session_state(self, session_id: str) -> dict[str, Any]:
        return await self._request_json("GET", f"/internal/session/state/{quote(session_id, safe='')}") or {}

    async def session_turns(self, session_id: str, *, limit: int = 20) -> dict[str, Any]:
        return await self._request_json(
            "GET",
            f"/internal/session/turns/{quote(session_id, safe='')}",
            params={"limit": max(1, min(int(limit), 200))},
        ) or {}

    async def session_history(
        self,
        session_id: str,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        return await self._request_json(
            "GET",
            f"/internal/session/history/{quote(session_id, safe='')}",
            params={"limit": max(1, min(int(limit), 200)), "offset": max(0, int(offset))},
        ) or {}

    async def task_notebook(self, task_id: str) -> dict[str, Any]:
        normalized = str(task_id or "").strip()
        if not normalized:
            raise ValueError("task_id is required")
        payload = await self._request_json(
            "GET",
            f"/internal/session/task-notebook/{quote(normalized, safe='')}",
            allow_404=True,
        )
        if payload is None:
            return {"found": False, "task_id": normalized}
        return {"found": True, "task_id": normalized, "notebook": payload}

    async def session_revisit(
        self,
        session_id: str,
        *,
        task_id: str | None = None,
        request_id: str | None = None,
        turn_limit: int = 8,
        raw_history_limit: int = 12,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_id": str(session_id or "").strip(),
            "turn_limit": max(1, min(int(turn_limit), 200)),
            "raw_history_limit": max(1, min(int(raw_history_limit), 200)),
        }
        if task_id:
            payload["task_id"] = str(task_id).strip()
        if request_id:
            payload["request_id"] = str(request_id).strip()
        return await self._request_json("POST", "/internal/session/revisit", json_body=payload) or {}


class MemoryWrite(_GatewayInternalClient):
    async def write(
        self,
        *,
        kind: str,
        title: str,
        content: str,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": str(kind or "").strip(),
            "title": str(title or "").strip(),
            "content": str(content or "").strip(),
            "writer_id": self.agent_id,
        }
        if tags:
            payload["tags"] = [item.strip() for item in tags if str(item or "").strip()]
        if isinstance(metadata, dict) and metadata:
            payload["metadata"] = metadata
        if isinstance(provenance, dict) and provenance:
            payload["provenance"] = provenance
        return await self._request_json("POST", "/internal/memory/write", json_body=payload) or {}

    async def write_core_fact(
        self,
        *,
        fact: str,
        title: str,
        canonical_key: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        provenance: dict[str, Any] | None = None,
        priority: int = 100,
        always_include: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "fact": str(fact or "").strip(),
            "title": str(title or "").strip(),
            "priority": max(0, min(int(priority), 1000)),
            "always_include": bool(always_include),
            "writer_id": self.agent_id,
        }
        if canonical_key:
            payload["canonical_key"] = str(canonical_key).strip()
        if tags:
            payload["tags"] = [item.strip() for item in tags if str(item or "").strip()]
        if isinstance(metadata, dict) and metadata:
            payload["metadata"] = metadata
        if isinstance(provenance, dict) and provenance:
            payload["provenance"] = provenance
        return await self._request_json("POST", "/internal/memory/core-facts", json_body=payload) or {}

    async def ingest_episode(
        self,
        *,
        observations: list[dict[str, Any]],
        provenance: dict[str, Any],
        title: str | None = None,
        kind: str = "transcript",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": str(kind or "").strip() or "transcript",
            "title": str(title or "").strip() or "Transcript episode",
            "observations": observations,
            "provenance": provenance,
            "writer_id": self.agent_id,
        }
        if tags:
            payload["tags"] = [item.strip() for item in tags if str(item or "").strip()]
        if isinstance(metadata, dict) and metadata:
            payload["metadata"] = metadata
        return await self._request_json("POST", "/internal/memory/episodes", json_body=payload) or {}
