from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass(slots=True)
class MemoryPromptContext:
    core_facts_rendered: str = ""
    recall_items: list[dict[str, Any]] = field(default_factory=list)
    total_token_count: int = 0
    rendered: str = ""
    diagnostics: dict[str, Any] | None = None


class MemoryClientError(RuntimeError):
    pass


class MemoryClientHTTPError(MemoryClientError):
    def __init__(self, *, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class CosmicMemoryClient:
    """Small async Gateway client for the standalone cosmic-memory service."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_sec: float,
        internal_token: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = timeout_sec
        self.internal_token = internal_token.strip()
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    async def start(self) -> None:
        if not self.enabled or self._client is not None:
            return
        timeout = httpx.Timeout(self.timeout_sec, connect=min(self.timeout_sec, 5.0))
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            http2=True,
        )

    async def stop(self) -> None:
        if self._client is None:
            return
        await self._client.aclose()
        self._client = None

    async def health(self) -> dict[str, Any]:
        if not self.enabled:
            return {
                "enabled": False,
                "status": "disabled",
            }
        try:
            payload = await self._request_json("GET", "/health")
        except Exception as exc:
            return {
                "enabled": True,
                "status": "error",
                "error": str(exc),
            }
        if isinstance(payload, dict):
            return {
                "enabled": True,
                **payload,
            }
        return {
            "enabled": True,
            "status": "ok",
        }

    async def build_prompt_context(
        self,
        *,
        query: str,
        max_results: int,
        token_budget: int,
        core_fact_max_chars: int,
        kinds: tuple[str, ...],
        include_diagnostics: bool = False,
    ) -> MemoryPromptContext:
        if not self.enabled:
            return MemoryPromptContext()
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return MemoryPromptContext()

        core_fact_task = asyncio.create_task(
            self._request_json(
                "GET",
                "/v1/core-facts",
                params={
                    "max_chars": max(250, core_fact_max_chars),
                },
            )
        )
        passive_task = asyncio.create_task(
            self._request_json(
                "POST",
                "/v1/query/passive",
                json_body={
                    "query": normalized_query,
                    "kinds": list(kinds),
                    "max_results": max(1, max_results),
                    "token_budget": max(256, token_budget),
                    "include_diagnostics": include_diagnostics,
                },
            )
        )

        core_facts_rendered = ""
        recall_items: list[dict[str, Any]] = []
        total_token_count = 0
        diagnostics: dict[str, Any] | None = None

        core_result, passive_result = await asyncio.gather(
            core_fact_task,
            passive_task,
            return_exceptions=True,
        )

        if isinstance(core_result, dict):
            core_facts_rendered = str(core_result.get("rendered") or "").strip()
        elif isinstance(core_result, Exception):
            raise MemoryClientError(f"Failed to fetch core facts: {core_result}") from core_result

        if isinstance(passive_result, dict):
            raw_items = passive_result.get("items")
            if isinstance(raw_items, list):
                recall_items = [item for item in raw_items if isinstance(item, dict)]
            total_token_count = int(passive_result.get("total_token_count") or 0)
            raw_diagnostics = passive_result.get("diagnostics")
            diagnostics = raw_diagnostics if isinstance(raw_diagnostics, dict) else None
        elif isinstance(passive_result, Exception):
            raise MemoryClientError(f"Failed to fetch passive recall: {passive_result}") from passive_result

        rendered = self._render_prompt_context(
            core_facts_rendered=core_facts_rendered,
            recall_items=recall_items,
        )
        return MemoryPromptContext(
            core_facts_rendered=core_facts_rendered,
            recall_items=recall_items,
            total_token_count=total_token_count,
            rendered=rendered,
            diagnostics=diagnostics,
        )

    async def passive_search(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request_json("POST", "/v1/query/passive", json_body=payload)

    async def active_search(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request_json("POST", "/v1/query/active", json_body=payload)

    async def write_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request_json("POST", "/v1/memories", json_body=payload)

    async def write_core_fact(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request_json("POST", "/v1/core-facts", json_body=payload)

    async def ingest_episode(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request_json("POST", "/v1/episodes", json_body=payload)

    async def get_schema_context(self) -> dict[str, Any]:
        return await self._request_json("GET", "/v1/agent/schema-context")

    async def plan_query(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request_json("POST", "/v1/agent/plan", json_body=payload)

    async def resolve_identity(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request_json("POST", "/v1/agent/resolve-identity", json_body=payload)

    async def current_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request_json("POST", "/v1/agent/current-state", json_body=payload)

    async def temporal_facts(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request_json("POST", "/v1/agent/temporal-facts", json_body=payload)

    async def memory_brief(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request_json("POST", "/v1/agent/memory-brief", json_body=payload)

    async def index_status(self) -> dict[str, Any]:
        return await self._request_json("GET", "/v1/index/status")

    async def index_sync(self) -> dict[str, Any]:
        return await self._request_json("POST", "/v1/index/sync", json_body={})

    async def index_rebuild(self) -> dict[str, Any]:
        return await self._request_json("POST", "/v1/index/rebuild", json_body={})

    async def get_core_fact_block(self, *, max_chars: int = 1500) -> dict[str, Any]:
        return await self._request_json(
            "GET",
            "/v1/core-facts",
            params={"max_chars": max(250, max_chars)},
        )

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise MemoryClientError("cosmic-memory is not configured")
        if self._client is None:
            await self.start()
        if self._client is None:
            raise MemoryClientError("cosmic-memory client is not initialized")

        headers: dict[str, str] = {}
        if self.internal_token:
            headers["X-Internal-Token"] = self.internal_token

        response = await self._client.request(
            method,
            path,
            json=json_body,
            params=params,
            headers=headers,
        )
        if response.status_code >= 400:
            raise MemoryClientHTTPError(
                status_code=response.status_code,
                message=self._error_from_response(response),
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise MemoryClientError("cosmic-memory returned a non-object payload")
        return payload

    def _error_from_response(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            text = response.text.strip()
            return text or f"status={response.status_code}"

        if isinstance(payload, dict):
            detail = payload.get("detail")
            if isinstance(detail, str) and detail.strip():
                return detail.strip()
            error = payload.get("error")
            if isinstance(error, str) and error.strip():
                return error.strip()
        return f"status={response.status_code}"

    def _render_prompt_context(
        self,
        *,
        core_facts_rendered: str,
        recall_items: list[dict[str, Any]],
    ) -> str:
        lines: list[str] = [
            "Relevant long-term memory context for this request.",
            "Use it when it helps answer the user accurately.",
            "Prefer current, directly relevant memories over broad older summaries.",
            "Do not mention internal memory bookkeeping unless the user explicitly asks.",
        ]

        if core_facts_rendered:
            lines.extend(
                [
                    "",
                    "Always-on core facts:",
                    core_facts_rendered,
                ]
            )

        if recall_items:
            lines.extend(["", "Retrieved long-term memories:"])
            for index, item in enumerate(recall_items, start=1):
                kind = str(item.get("kind") or "memory").strip()
                title = str(item.get("title") or "").strip()
                heading = f"{index}. [{kind}]"
                if title:
                    heading += f" {title}"
                lines.append(heading)
                content = str(item.get("content") or "").strip()
                if content:
                    lines.append(content)

        if not core_facts_rendered and not recall_items:
            return ""
        return "\n".join(lines).strip()
