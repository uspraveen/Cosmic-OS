"""Tool executor for the COSMIC orchestrator agentic loop.

Executes tool calls made by Opus during the agentic loop. Each tool maps to
an internal service (cosmic-memory, Perplexity, Gateway scheduler) accessed
over the local network within the same VM.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class ToolExecutor:
    """Executes orchestrator tool calls against internal COSMIC services."""

    def __init__(
        self,
        *,
        perplexity_api_key: str = "",
        perplexity_model: str = "sonar",
        cosmic_memory_url: str = "",
        gateway_url: str = "",
        gateway_internal_token: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.perplexity_api_key = perplexity_api_key.strip()
        self.perplexity_model = perplexity_model.strip() or "sonar"
        self.cosmic_memory_url = cosmic_memory_url.rstrip("/") if cosmic_memory_url else ""
        self.gateway_url = gateway_url.rstrip("/") if gateway_url else ""
        self.gateway_internal_token = gateway_internal_token.strip()
        timeout = httpx.Timeout(30.0, connect=10.0)
        self._client = client or httpx.AsyncClient(timeout=timeout, http2=True)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def execute(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Execute a tool call and return the result as a string."""
        started_at = time.perf_counter()
        try:
            result = await self._dispatch(tool_name, tool_input)
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            logger.info("tool.executed name=%s rtt_ms=%d", tool_name, elapsed_ms)
            return result
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            logger.warning(
                "tool.failed name=%s rtt_ms=%d error=%s",
                tool_name, elapsed_ms, str(exc)[:200],
            )
            return json.dumps({
                "error": True,
                "tool": tool_name,
                "message": str(exc).strip()[:500] or "Tool execution failed.",
            })

    async def _dispatch(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Route a tool call to the appropriate handler."""
        if tool_name == "web_search":
            return await self._web_search(tool_input)
        if tool_name == "memory_search":
            return await self._memory_search(tool_input)
        if tool_name == "memory_write":
            return await self._memory_write(tool_input)
        if tool_name == "create_reminder":
            return await self._create_reminder(tool_input)
        if tool_name == "list_reminders":
            return await self._list_reminders()
        if tool_name == "delete_reminder":
            return await self._delete_reminder(tool_input)
        return json.dumps({"error": True, "message": f"Unknown tool: {tool_name}"})

    # ── Web Search (Perplexity) ─────────────────────────────────

    async def _web_search(self, tool_input: dict[str, Any]) -> str:
        query = str(tool_input.get("query") or "").strip()
        if not query:
            return json.dumps({"error": True, "message": "query is required"})
        if not self.perplexity_api_key:
            return json.dumps({"error": True, "message": "Web search is not configured (no Perplexity API key)."})

        response = await self._client.post(
            "https://api.perplexity.ai/chat/completions",
            headers={
                "Authorization": f"Bearer {self.perplexity_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.perplexity_model,
                "messages": [
                    {"role": "system", "content": "Be precise and informative. Cite sources when possible."},
                    {"role": "user", "content": query},
                ],
                "max_tokens": 1500,
            },
        )
        if response.status_code >= 400:
            body = response.text[:300]
            return json.dumps({"error": True, "message": f"Perplexity API error (status={response.status_code}): {body}"})

        payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            return json.dumps({"error": True, "message": "No results from web search."})

        answer = str(choices[0].get("message", {}).get("content") or "").strip()
        citations = payload.get("citations") or []
        result: dict[str, Any] = {"answer": answer}
        if citations:
            result["citations"] = citations[:10]
        return json.dumps(result, ensure_ascii=False)

    # ── Memory Search ───────────────────────────────────────────

    async def _memory_search(self, tool_input: dict[str, Any]) -> str:
        query = str(tool_input.get("query") or "").strip()
        if not query:
            return json.dumps({"error": True, "message": "query is required"})
        if not self.cosmic_memory_url:
            return json.dumps({"error": True, "message": "Memory service is not configured."})

        max_results = int(tool_input.get("max_results") or 5)
        response = await self._client.post(
            f"{self.cosmic_memory_url}/v1/query/active",
            json={
                "query": query,
                "max_results": min(max(1, max_results), 20),
                "token_budget": 3000,
            },
            headers=self._memory_headers(),
        )
        if response.status_code >= 400:
            return json.dumps({"error": True, "message": f"Memory search failed (status={response.status_code})"})

        payload = response.json()
        items = payload.get("items") or []
        if not items:
            return json.dumps({"results": [], "message": "No matching memories found."})

        results = []
        for item in items:
            if not isinstance(item, dict):
                continue
            results.append({
                "kind": str(item.get("kind") or "memory"),
                "title": str(item.get("title") or "").strip(),
                "content": str(item.get("content") or "").strip()[:1000],
                "score": item.get("score"),
            })
        return json.dumps({"results": results}, ensure_ascii=False)

    # ── Memory Write ────────────────────────────────────────────

    async def _memory_write(self, tool_input: dict[str, Any]) -> str:
        content = str(tool_input.get("content") or "").strip()
        kind = str(tool_input.get("kind") or "note").strip()
        title = str(tool_input.get("title") or "").strip()
        if not content:
            return json.dumps({"error": True, "message": "content is required"})
        if not self.cosmic_memory_url:
            return json.dumps({"error": True, "message": "Memory service is not configured."})

        response = await self._client.post(
            f"{self.cosmic_memory_url}/v1/memories",
            json={
                "content": content,
                "kind": kind,
                "title": title or kind,
                "source": "orchestrator",
            },
            headers=self._memory_headers(),
        )
        if response.status_code >= 400:
            body = response.text[:200]
            return json.dumps({"error": True, "message": f"Memory write failed (status={response.status_code}): {body}"})

        payload = response.json()
        return json.dumps({
            "saved": True,
            "id": payload.get("id") or payload.get("memory_id"),
            "message": f"Memory saved: {title}",
        }, ensure_ascii=False)

    # ── Create Reminder (Gateway Scheduler) ─────────────────────

    async def _create_reminder(self, tool_input: dict[str, Any]) -> str:
        label = str(tool_input.get("label") or "").strip()
        cron_expression = str(tool_input.get("cron_expression") or "").strip()
        prompt = str(tool_input.get("prompt") or "").strip()
        one_shot = bool(tool_input.get("one_shot", True))
        if not label or not cron_expression or not prompt:
            return json.dumps({"error": True, "message": "label, cron_expression, and prompt are required"})
        if not self.gateway_url:
            return json.dumps({"error": True, "message": "Gateway scheduler is not configured."})

        response = await self._client.post(
            f"{self.gateway_url}/internal/scheduler/crons",
            json={
                "label": label,
                "cron_expression": cron_expression,
                "prompt": prompt,
                "one_shot": one_shot,
                "source": "orchestrator",
            },
            headers=self._gateway_headers(),
        )
        if response.status_code >= 400:
            body = response.text[:200]
            return json.dumps({"error": True, "message": f"Scheduler error (status={response.status_code}): {body}"})

        payload = response.json()
        return json.dumps({
            "created": True,
            "cron_id": payload.get("cron_id"),
            "label": label,
            "cron_expression": cron_expression,
            "one_shot": one_shot,
            "message": f"Reminder created: {label}",
        }, ensure_ascii=False)

    # ── List Reminders ──────────────────────────────────────────

    async def _list_reminders(self) -> str:
        if not self.gateway_url:
            return json.dumps({"error": True, "message": "Gateway scheduler is not configured."})

        response = await self._client.get(
            f"{self.gateway_url}/internal/scheduler/crons",
            headers=self._gateway_headers(),
        )
        if response.status_code >= 400:
            return json.dumps({"error": True, "message": f"Scheduler error (status={response.status_code})"})

        payload = response.json()
        crons = payload.get("crons") or []
        return json.dumps({"reminders": crons}, ensure_ascii=False)

    # ── Delete Reminder ─────────────────────────────────────────

    async def _delete_reminder(self, tool_input: dict[str, Any]) -> str:
        cron_id = str(tool_input.get("cron_id") or "").strip()
        if not cron_id:
            return json.dumps({"error": True, "message": "cron_id is required"})
        if not self.gateway_url:
            return json.dumps({"error": True, "message": "Gateway scheduler is not configured."})

        response = await self._client.delete(
            f"{self.gateway_url}/internal/scheduler/crons/{cron_id}",
            headers=self._gateway_headers(),
        )
        if response.status_code >= 400:
            return json.dumps({"error": True, "message": f"Delete failed (status={response.status_code})"})

        return json.dumps({"deleted": True, "cron_id": cron_id, "message": "Reminder deleted."})

    # ── Internal helpers ────────────────────────────────────────

    def _memory_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.gateway_internal_token:
            headers["X-Internal-Token"] = self.gateway_internal_token
        return headers

    def _gateway_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.gateway_internal_token:
            headers["X-Internal-Token"] = self.gateway_internal_token
        return headers
