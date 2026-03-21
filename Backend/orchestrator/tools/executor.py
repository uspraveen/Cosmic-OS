"""Tool executor for the COSMIC orchestrator agentic loop.

Executes tool calls made by Opus during the agentic loop. Each tool maps to
an internal COSMIC service or an external research provider.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx

from shared import begin_metered_call, build_model_key, build_usage_event, post_usage_event
from shared.contracts import AgentResult, TaskEnvelope, TaskInProgress

from .registry import get_local_tool_spec

logger = logging.getLogger(__name__)


class ToolHTTPError(RuntimeError):
    def __init__(self, *, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    task_id: str | None = None
    request_id: str | None = None
    session_id: str | None = None
    channel: str | None = None
    source: str | None = None
    source_id: str | None = None
    parent_task: TaskEnvelope | None = None


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
        usage_source_id: str = "orchestrator:tool_executor",
        agent_dispatcher: Callable[..., Awaitable[AgentResult | TaskInProgress]] | None = None,
        agent_catalog_searcher: Callable[..., Awaitable[dict[str, Any]]] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.perplexity_api_key = perplexity_api_key.strip()
        self.perplexity_model = perplexity_model.strip() or "sonar"
        self.cosmic_memory_url = cosmic_memory_url.rstrip("/") if cosmic_memory_url else ""
        self.gateway_url = gateway_url.rstrip("/") if gateway_url else ""
        self.gateway_internal_token = gateway_internal_token.strip()
        self.usage_source_id = usage_source_id.strip() or "orchestrator:tool_executor"
        self._agent_dispatcher = agent_dispatcher
        self._agent_catalog_searcher = agent_catalog_searcher
        timeout = httpx.Timeout(30.0, connect=10.0)
        self._client = client or httpx.AsyncClient(timeout=timeout, http2=True)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def execute(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> str:
        """Execute a tool call and return the result as a JSON string."""
        started_at = time.perf_counter()
        try:
            result = await self._dispatch(tool_name, tool_input, context=context)
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            logger.info("tool.executed name=%s rtt_ms=%d", tool_name, elapsed_ms)
            if isinstance(result, str):
                return result
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            logger.warning(
                "tool.failed name=%s rtt_ms=%d error=%s",
                tool_name, elapsed_ms, str(exc)[:200],
            )
            return json.dumps(
                {
                    "error": True,
                    "tool": tool_name,
                    "message": str(exc).strip()[:500] or "Tool execution failed.",
                }
            )

    async def _dispatch(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any] | list[Any] | str:
        spec = get_local_tool_spec(tool_name)
        if spec is None or not spec.handler_method:
            return {"error": True, "message": f"Unknown or unsupported tool: {tool_name}"}
        handler = getattr(self, spec.handler_method, None)
        if handler is None:
            return {"error": True, "message": f"Tool handler is not implemented: {tool_name}"}
        return await handler(tool_input, context=context)

    # ── Perplexity Research ──────────────────────────────────────

    async def _perplexity_research(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        query = str(tool_input.get("query") or "").strip()
        if not query:
            return {"error": True, "message": "query is required"}
        if not self.perplexity_api_key:
            return {"error": True, "message": "Web search is not configured (no Perplexity API key)."}

        metered_call = begin_metered_call(prefix="call")
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
            await self._post_usage_event(
                build_usage_event(
                    metered_call=metered_call,
                    source_component="orchestrator",
                    source_id=self.usage_source_id,
                    task_id=context.task_id if context else None,
                    session_id=context.session_id if context else None,
                    route="opus",
                    operation="orchestrator.perplexity_research",
                    model_key=build_model_key("perplexity", self.perplexity_model),
                    request_id=context.request_id if context else None,
                    provider_request_id=(
                        response.headers.get("x-request-id")
                        or response.headers.get("request-id")
                        or response.headers.get("x-perplexity-request-id")
                        or None
                    ),
                    raw_usage=response.json().get("usage") if response.headers.get("content-type", "").startswith("application/json") else None,
                    success=False,
                    error_code=f"HTTP_{response.status_code}",
                    metadata_json={
                        "tool": "perplexity_research",
                        "status_code": response.status_code,
                    },
                )
            )
            return {"error": True, "message": f"Perplexity API error (status={response.status_code}): {body}"}

        payload = response.json()
        await self._post_usage_event(
            build_usage_event(
                metered_call=metered_call,
                source_component="orchestrator",
                source_id=self.usage_source_id,
                task_id=context.task_id if context else None,
                session_id=context.session_id if context else None,
                route="opus",
                operation="orchestrator.perplexity_research",
                model_key=build_model_key("perplexity", self.perplexity_model),
                request_id=context.request_id if context else None,
                provider_request_id=(
                    response.headers.get("x-request-id")
                    or response.headers.get("request-id")
                    or response.headers.get("x-perplexity-request-id")
                    or None
                ),
                raw_usage=payload.get("usage"),
                success=True,
                metadata_json={
                    "tool": "perplexity_research",
                },
            )
        )
        choices = payload.get("choices") or []
        if not choices:
            return {"error": True, "message": "No results from web search."}

        answer = str(choices[0].get("message", {}).get("content") or "").strip()
        citations = payload.get("citations") or []
        result: dict[str, Any] = {"answer": answer}
        if citations:
            result["citations"] = citations[:10]
        return result

    async def _post_usage_event(self, event) -> None:
        try:
            posted = await post_usage_event(
                client=self._client,
                gateway_url=self.gateway_url,
                internal_token=self.gateway_internal_token,
                event=event,
            )
            if not posted:
                logger.warning(
                    "tool.usage_post_failed llm_call_id=%s operation=%s model=%s",
                    event.llm_call_id,
                    event.operation,
                    event.model,
                )
        except Exception:
            logger.exception(
                "tool.usage_post_exception llm_call_id=%s operation=%s model=%s",
                event.llm_call_id,
                event.operation,
                event.model,
            )

    # ── Specialist Agents ────────────────────────────────────────

    async def _agent_catalog_search(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        del context
        if self._agent_catalog_searcher is None:
            return {"error": True, "message": "Agent catalog search is not configured in this orchestrator runtime."}
        query = str(tool_input.get("query") or "").strip()
        limit = min(max(1, self._coerce_int(tool_input.get("limit"), 5)), 20)
        require_healthy = self._coerce_bool(tool_input.get("require_healthy"), default=True)
        return await self._agent_catalog_searcher(
            query=query,
            limit=limit,
            require_healthy=require_healthy,
        )

    async def _delegate_to_agent(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        intent = str(tool_input.get("intent") or "").strip()
        if not intent:
            return {"error": True, "message": "intent is required"}
        payload = tool_input.get("input")
        if not isinstance(payload, dict):
            return {"error": True, "message": "input must be an object"}
        preferred_agent_id = str(tool_input.get("agent_id") or "").strip() or None
        wait_timeout_value = tool_input.get("wait_timeout_sec")
        wait_timeout_sec: float | None = None
        if wait_timeout_value not in (None, ""):
            try:
                wait_timeout_sec = max(1.0, float(wait_timeout_value))
            except (TypeError, ValueError):
                return {"error": True, "message": "wait_timeout_sec must be a number when provided"}
        response = await self._dispatch_specialist_agent(
            intent=intent,
            payload=payload,
            context=context,
            agent_id=preferred_agent_id,
            wait_timeout_sec=wait_timeout_sec,
        )
        if "delegation" not in response:
            response["delegation"] = {
                "intent": intent,
                "agent_id": preferred_agent_id,
            }
        return response

    async def _cosmics_capability_wishlist_search(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        del context
        query = str(tool_input.get("query") or "").strip()
        if not query:
            return {"error": True, "message": "query is required"}
        limit = min(max(1, self._coerce_int(tool_input.get("limit"), 3)), 10)
        payload = await self._request_gateway_json(
            "POST",
            "/internal/cosmics-capability-wishlist/search",
            json_body={"query": query, "limit": limit},
        )
        if payload is None:
            return {"error": True, "message": "Capability wishlist search did not return a payload."}
        return payload

    async def _cosmics_capability_wishlist_capture(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        title = str(tool_input.get("title") or "").strip()
        summary = str(tool_input.get("summary") or "").strip()
        if not title:
            return {"error": True, "message": "title is required"}
        if not summary:
            return {"error": True, "message": "summary is required"}
        payload: dict[str, Any] = {
            "title": title,
            "summary": summary,
            "desired_outcome": str(tool_input.get("desired_outcome") or "").strip() or None,
            "domain": str(tool_input.get("domain") or "").strip() or None,
            "tags": self._normalize_string_list(tool_input.get("tags")),
            "evidence": str(tool_input.get("evidence") or "").strip() or None,
            "source_component": "orchestrator",
            "source_id": context.source_id if context else None,
            "request_id": context.request_id if context else None,
            "session_id": context.session_id if context else None,
            "task_id": context.task_id if context else None,
            "route": "opus",
            "created_by": "cosmic/orchestrator:1.0.0",
            "metadata": self._clean_mapping(
                {
                    "task_source": context.source if context else None,
                    "channel": context.channel if context else None,
                }
            ),
        }
        gateway_payload = await self._request_gateway_json(
            "POST",
            "/internal/cosmics-capability-wishlist/capture",
            json_body=payload,
        )
        if gateway_payload is None:
            return {"error": True, "message": "Capability wishlist capture did not return a payload."}
        return gateway_payload

    async def _docs_browse(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        bundle_id = str(tool_input.get("bundle_id") or "").strip()
        if not bundle_id:
            return {"error": True, "message": "bundle_id is required"}
        payload: dict[str, Any] = {
            "bundle_id": bundle_id,
            "index_kind": str(tool_input.get("index_kind") or "").strip() or "documents",
        }
        doc_id = str(tool_input.get("doc_id") or "").strip()
        if doc_id:
            payload["doc_id"] = doc_id
        limit = self._coerce_int(tool_input.get("limit"), 20)
        if limit > 0:
            payload["limit"] = min(max(limit, 1), 100)
        return await self._dispatch_specialist_agent(
            intent="docs.browse_bundle",
            payload=payload,
            context=context,
            agent_id="cosmic/docs-parser-agent:1.0.0",
            wait_timeout_sec=35.0,
        )

    async def _docs_search(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        bundle_id = str(tool_input.get("bundle_id") or "").strip()
        query = str(tool_input.get("query") or "").strip()
        if not bundle_id:
            return {"error": True, "message": "bundle_id is required"}
        if not query:
            return {"error": True, "message": "query is required"}
        payload: dict[str, Any] = {"bundle_id": bundle_id, "query": query}
        search_kind = str(tool_input.get("search_kind") or "").strip()
        if search_kind:
            payload["search_kind"] = search_kind
        doc_ids = self._normalize_string_list(tool_input.get("doc_ids"))
        if doc_ids:
            payload["doc_ids"] = doc_ids[:12]
        limit = self._coerce_int(tool_input.get("limit"), 5)
        if limit > 0:
            payload["limit"] = min(max(limit, 1), 12)
        return await self._dispatch_specialist_agent(
            intent="docs.search_bundle",
            payload=payload,
            context=context,
            agent_id="cosmic/docs-parser-agent:1.0.0",
            wait_timeout_sec=35.0,
        )

    async def _docs_read(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        bundle_id = str(tool_input.get("bundle_id") or "").strip()
        if not bundle_id:
            return {"error": True, "message": "bundle_id is required"}
        payload: dict[str, Any] = {"bundle_id": bundle_id}
        doc_id = str(tool_input.get("doc_id") or "").strip()
        if doc_id:
            payload["doc_id"] = doc_id
        read_kind = str(tool_input.get("read_kind") or "").strip()
        if read_kind:
            payload["read_kind"] = read_kind
        section_id = str(tool_input.get("section_id") or "").strip()
        if section_id:
            payload["section_id"] = section_id
        chunk_ids = self._normalize_string_list(tool_input.get("chunk_ids"))
        if chunk_ids:
            payload["chunk_ids"] = chunk_ids[:8]
        for key in ("start_page", "end_page", "start_slide", "end_slide", "offset_chars", "before_chars", "after_chars"):
            value = self._coerce_int(tool_input.get(key), 0)
            if value > 0:
                payload[key] = value
        anchor_id = str(tool_input.get("anchor_id") or "").strip()
        if anchor_id:
            payload["anchor_id"] = anchor_id
        max_chars = self._coerce_int(tool_input.get("max_chars"), 5000)
        if max_chars > 0:
            payload["max_chars"] = min(max(max_chars, 500), 12000)
        return await self._dispatch_specialist_agent(
            intent="docs.read_bundle",
            payload=payload,
            context=context,
            agent_id="cosmic/docs-parser-agent:1.0.0",
            wait_timeout_sec=35.0,
        )

    async def _docs_fetch_asset(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        bundle_id = str(tool_input.get("bundle_id") or "").strip()
        asset_id = str(tool_input.get("asset_id") or "").strip()
        if not bundle_id:
            return {"error": True, "message": "bundle_id is required"}
        if not asset_id:
            return {"error": True, "message": "asset_id is required"}
        payload: dict[str, Any] = {"bundle_id": bundle_id, "asset_id": asset_id}
        doc_id = str(tool_input.get("doc_id") or "").strip()
        if doc_id:
            payload["doc_id"] = doc_id
        max_chars = self._coerce_int(tool_input.get("max_chars"), 5000)
        if max_chars > 0:
            payload["max_chars"] = min(max(max_chars, 500), 12000)
        return await self._dispatch_specialist_agent(
            intent="docs.fetch_asset",
            payload=payload,
            context=context,
            agent_id="cosmic/docs-parser-agent:1.0.0",
            wait_timeout_sec=35.0,
        )

    async def _docs_reinspect_asset(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        bundle_id = str(tool_input.get("bundle_id") or "").strip()
        asset_id = str(tool_input.get("asset_id") or "").strip()
        if not bundle_id:
            return {"error": True, "message": "bundle_id is required"}
        if not asset_id:
            return {"error": True, "message": "asset_id is required"}
        payload: dict[str, Any] = {"bundle_id": bundle_id, "asset_id": asset_id}
        doc_id = str(tool_input.get("doc_id") or "").strip()
        if doc_id:
            payload["doc_id"] = doc_id
        question = str(tool_input.get("question") or "").strip()
        if question:
            payload["question"] = question
        return await self._dispatch_specialist_agent(
            intent="docs.reinspect_asset",
            payload=payload,
            context=context,
            agent_id="cosmic/docs-parser-agent:1.0.0",
            wait_timeout_sec=45.0,
        )

    async def _firecrawl_scrape(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "url": str(tool_input.get("url") or "").strip(),
        }
        formats = self._normalize_string_list(tool_input.get("formats"))
        if formats:
            payload["formats"] = formats
        for key in (
            "only_main_content",
            "wait_for_ms",
            "timeout_ms",
            "max_age_ms",
            "include_tags",
            "exclude_tags",
            "mobile",
            "proxy",
        ):
            value = tool_input.get(key)
            if value not in (None, "", [], {}):
                payload[key] = value
        return await self._dispatch_specialist_agent(
            intent="firecrawl.scrape",
            payload=payload,
            context=context,
            agent_id="cosmic/firecrawl-web-scrape-agent:1.0.0",
            wait_timeout_sec=125.0,
        )

    async def _firecrawl_extract(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "urls": self._normalize_string_list(tool_input.get("urls")),
            "prompt": str(tool_input.get("prompt") or "").strip(),
        }
        for key in (
            "schema",
            "show_sources",
            "enable_web_search",
            "only_main_content",
            "wait_for_ms",
            "timeout_ms",
            "max_age_ms",
        ):
            value = tool_input.get(key)
            if value not in (None, "", [], {}):
                payload[key] = value
        return await self._dispatch_specialist_agent(
            intent="firecrawl.extract",
            payload=payload,
            context=context,
            agent_id="cosmic/firecrawl-web-scrape-agent:1.0.0",
            wait_timeout_sec=185.0,
        )

    async def _firecrawl_recall_session(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_id": str(tool_input.get("session_id") or "").strip() or (context.session_id if context else ""),
        }
        limit = self._coerce_int(tool_input.get("limit"), 10)
        if limit > 0:
            payload["limit"] = min(max(limit, 1), 50)
        return await self._dispatch_specialist_agent(
            intent="firecrawl.recall_session",
            payload=payload,
            context=context,
            agent_id="cosmic/firecrawl-web-scrape-agent:1.0.0",
            wait_timeout_sec=35.0,
        )

    async def _x_search(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        query = str(tool_input.get("query") or "").strip()
        if not query:
            return {"error": True, "message": "query is required"}
        payload: dict[str, Any] = {"query": query}
        analysis_goal = str(tool_input.get("analysis_goal") or "").strip()
        if analysis_goal:
            payload["analysis_goal"] = analysis_goal
        allowed_x_handles = self._normalize_string_list(tool_input.get("allowed_x_handles"))
        if allowed_x_handles:
            payload["allowed_x_handles"] = allowed_x_handles[:10]
        excluded_x_handles = self._normalize_string_list(tool_input.get("excluded_x_handles"))
        if excluded_x_handles:
            payload["excluded_x_handles"] = excluded_x_handles[:10]
        from_date = str(tool_input.get("from_date") or "").strip()
        if from_date:
            payload["from_date"] = from_date
        to_date = str(tool_input.get("to_date") or "").strip()
        if to_date:
            payload["to_date"] = to_date
        if "enable_image_understanding" in tool_input:
            payload["enable_image_understanding"] = self._coerce_bool(
                tool_input.get("enable_image_understanding"),
                default=False,
            )
        if "enable_video_understanding" in tool_input:
            payload["enable_video_understanding"] = self._coerce_bool(
                tool_input.get("enable_video_understanding"),
                default=False,
            )
        max_posts = self._coerce_int(tool_input.get("max_posts"), 8)
        if max_posts > 0:
            payload["max_posts"] = min(max(max_posts, 1), 8)
        return await self._dispatch_specialist_agent(
            intent="x.search",
            payload=payload,
            context=context,
            agent_id="cosmic/x-twitter-search-agent:1.0.0",
            wait_timeout_sec=35.0,
        )

    async def _x_recall_session(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        session_id = str(tool_input.get("session_id") or "").strip() or (context.session_id if context else "")
        if not session_id:
            return {"error": True, "message": "session_id is required"}
        limit = self._coerce_int(tool_input.get("limit"), 10)
        return await self._dispatch_specialist_agent(
            intent="x.recall_session",
            payload={"session_id": session_id, "limit": min(max(limit, 1), 50)},
            context=context,
            agent_id="cosmic/x-twitter-search-agent:1.0.0",
            wait_timeout_sec=20.0,
        )

    # ── Memory Search / Write ────────────────────────────────────

    async def _memory_search(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        del context
        query = str(tool_input.get("query") or "").strip()
        if not query:
            return {"error": True, "message": "query is required"}

        payload: dict[str, Any] = {
            "query": query,
            "max_results": min(max(1, self._coerce_int(tool_input.get("max_results"), 5)), 20),
            "token_budget": min(max(256, self._coerce_int(tool_input.get("token_budget"), 3000)), 12000),
        }
        kinds = self._normalize_string_list(tool_input.get("kinds"))
        if kinds:
            payload["kinds"] = kinds
        seed_memory_ids = self._normalize_string_list(tool_input.get("seed_memory_ids"))
        if seed_memory_ids:
            payload["seed_memory_ids"] = seed_memory_ids
        seed_entities = self._normalize_string_list(tool_input.get("seed_entities"))
        if seed_entities:
            payload["seed_entities"] = seed_entities
        max_hops = self._coerce_int(tool_input.get("max_hops"), 2)
        if max_hops > 0:
            payload["max_hops"] = min(max_hops, 6)
        if bool(tool_input.get("include_diagnostics")):
            payload["include_diagnostics"] = True

        if self.gateway_url:
            try:
                response_payload = await self._request_gateway_json(
                    "POST",
                    "/internal/memory/active-search",
                    json_body=payload,
                )
                return self._normalize_memory_search_result(response_payload)
            except ToolHTTPError as exc:
                if exc.status_code not in {404, 405} or not self.cosmic_memory_url:
                    raise

        if self.cosmic_memory_url:
            response_payload = await self._request_cosmic_memory_json(
                "POST",
                "/v1/query/active",
                json_body=payload,
            )
            return self._normalize_memory_search_result(response_payload)

        raise RuntimeError("Memory service is not configured.")

    async def _memory_fetch(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        del context
        memory_id = str(tool_input.get("memory_id") or "").strip()
        if not memory_id:
            return {"error": True, "message": "memory_id is required"}

        gateway_path = f"/internal/memory/memories/{quote(memory_id, safe='')}"
        memory_path = f"/v1/memories/{quote(memory_id, safe='')}"

        if self.gateway_url:
            try:
                response_payload = await self._request_gateway_json(
                    "GET",
                    gateway_path,
                    allow_404=True,
                )
                if response_payload is None:
                    return {
                        "found": False,
                        "memory_id": memory_id,
                        "message": "Memory not found.",
                    }
                return self._normalize_memory_record_result(response_payload, requested_memory_id=memory_id)
            except ToolHTTPError as exc:
                if exc.status_code not in {404, 405} or not self.cosmic_memory_url:
                    raise

        if self.cosmic_memory_url:
            response_payload = await self._request_cosmic_memory_json(
                "GET",
                memory_path,
                allow_404=True,
            )
            if response_payload is None:
                return {
                    "found": False,
                    "memory_id": memory_id,
                    "message": "Memory not found.",
                }
            return self._normalize_memory_record_result(response_payload, requested_memory_id=memory_id)

        raise RuntimeError("Memory service is not configured.")

    async def _memory_write(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        content = str(tool_input.get("content") or "").strip()
        original_kind = str(tool_input.get("kind") or "agent_note").strip() or "agent_note"
        kind = self._normalize_memory_write_kind(original_kind)
        title = str(tool_input.get("title") or "").strip() or self._derive_memory_title(content)
        if not content:
            return {"error": True, "message": "content is required"}
        if kind is None:
            return {
                "error": True,
                "message": "Unsupported memory_write kind. Use user_data or agent_note. Stable always-on facts should use memory_write_core_fact.",
            }

        metadata = tool_input.get("metadata") if isinstance(tool_input.get("metadata"), dict) else {}
        payload: dict[str, Any] = {
            "content": content,
            "kind": kind,
            "title": title,
        }
        tags = self._normalize_string_list(tool_input.get("tags"))
        if tags:
            payload["tags"] = tags
        merged_metadata = self._merge_dicts(
            self._build_memory_metadata(context),
            metadata,
        )
        if merged_metadata:
            payload["metadata"] = merged_metadata
        provenance = self._build_orchestrator_provenance(context)
        if provenance:
            payload["provenance"] = provenance

        if self.gateway_url:
            try:
                response_payload = await self._request_gateway_json(
                    "POST",
                    "/internal/memory/write",
                    json_body=payload,
                )
                return {
                    "saved": True,
                    "deduplicated": bool(response_payload.get("deduplicated")),
                    "id": response_payload.get("memory_id") or response_payload.get("id"),
                    "message": (
                        f"Memory already captured: {title}"
                        if bool(response_payload.get("deduplicated"))
                        else f"Memory saved: {title}"
                    ),
                    "kind": kind,
                    "original_kind": original_kind,
                    "record": response_payload,
                }
            except ToolHTTPError as exc:
                if exc.status_code not in {404, 405} or not self.cosmic_memory_url:
                    raise

        if self.cosmic_memory_url:
            response_payload = await self._request_cosmic_memory_json(
                "POST",
                "/v1/memories",
                json_body=payload,
            )
            return {
                "saved": True,
                "deduplicated": bool(response_payload.get("deduplicated")),
                "id": response_payload.get("memory_id") or response_payload.get("id"),
                "message": (
                    f"Memory already captured: {title}"
                    if bool(response_payload.get("deduplicated"))
                    else f"Memory saved: {title}"
                ),
                "kind": kind,
                "original_kind": original_kind,
                "record": response_payload,
            }

        raise RuntimeError("Memory service is not configured.")

    async def _memory_write_core_fact(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        fact = str(tool_input.get("fact") or tool_input.get("content") or "").strip()
        title = str(tool_input.get("title") or "").strip() or self._derive_memory_title(fact)
        if not fact:
            return {"error": True, "message": "fact is required"}

        metadata = tool_input.get("metadata") if isinstance(tool_input.get("metadata"), dict) else {}
        payload: dict[str, Any] = {
            "fact": fact,
            "title": title,
            "priority": min(max(self._coerce_int(tool_input.get("priority"), 100), 0), 1000),
            "always_include": self._coerce_bool(tool_input.get("always_include"), True),
        }
        canonical_key = str(tool_input.get("canonical_key") or "").strip()
        if canonical_key:
            payload["canonical_key"] = canonical_key
        tags = self._normalize_string_list(tool_input.get("tags"))
        if tags:
            payload["tags"] = tags
        merged_metadata = self._merge_dicts(
            self._build_memory_metadata(context),
            metadata,
        )
        if merged_metadata:
            payload["metadata"] = merged_metadata
        provenance = self._build_orchestrator_provenance(context)
        if provenance:
            payload["provenance"] = provenance

        if self.gateway_url:
            try:
                response_payload = await self._request_gateway_json(
                    "POST",
                    "/internal/memory/core-facts",
                    json_body=payload,
                )
                return {
                    "saved": True,
                    "deduplicated": bool(response_payload.get("deduplicated")),
                    "id": response_payload.get("memory_id") or response_payload.get("id"),
                    "kind": "core_fact",
                    "title": title,
                    "canonical_key": canonical_key,
                    "message": (
                        f"Core fact already captured: {title}"
                        if bool(response_payload.get("deduplicated"))
                        else f"Core fact saved: {title}"
                    ),
                    "record": response_payload,
                }
            except ToolHTTPError as exc:
                if exc.status_code not in {404, 405} or not self.cosmic_memory_url:
                    raise

        if self.cosmic_memory_url:
            response_payload = await self._request_cosmic_memory_json(
                "POST",
                "/v1/core-facts",
                json_body=payload,
            )
            return {
                "saved": True,
                "deduplicated": bool(response_payload.get("deduplicated")),
                "id": response_payload.get("memory_id") or response_payload.get("id"),
                "kind": "core_fact",
                "title": title,
                "canonical_key": canonical_key,
                "message": (
                    f"Core fact already captured: {title}"
                    if bool(response_payload.get("deduplicated"))
                    else f"Core fact saved: {title}"
                ),
                "record": response_payload,
            }

        raise RuntimeError("Memory service is not configured.")

    # ── Session / Task Revisit ───────────────────────────────────

    async def _session_state(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        session_id = self._coerce_session_id(tool_input, context)
        if not session_id:
            return {"error": True, "message": "session_id is required"}
        return await self._request_gateway_json("GET", f"/internal/session/state/{session_id}")

    async def _session_turns(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        session_id = self._coerce_session_id(tool_input, context)
        if not session_id:
            return {"error": True, "message": "session_id is required"}
        limit = min(max(1, self._coerce_int(tool_input.get("limit"), 20)), 200)
        return await self._request_gateway_json(
            "GET",
            f"/internal/session/turns/{session_id}",
            params={"limit": limit},
        )

    async def _session_history(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        session_id = self._coerce_session_id(tool_input, context)
        if not session_id:
            return {"error": True, "message": "session_id is required"}
        limit = min(max(1, self._coerce_int(tool_input.get("limit"), 20)), 200)
        offset = max(0, self._coerce_int(tool_input.get("offset"), 0))
        return await self._request_gateway_json(
            "GET",
            f"/internal/session/history/{session_id}",
            params={"limit": limit, "offset": offset},
        )

    async def _task_notebook(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        task_id = self._coerce_task_id(tool_input, context)
        if not task_id:
            return {"error": True, "message": "task_id is required"}
        payload = await self._request_gateway_json(
            "GET",
            f"/internal/session/task-notebook/{task_id}",
            allow_404=True,
        )
        if payload is None:
            return {
                "found": False,
                "task_id": task_id,
                "message": "Task notebook not found.",
            }
        return {
            "found": True,
            "task_id": task_id,
            "notebook": payload,
        }

    async def _session_revisit(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        session_id = self._coerce_session_id(tool_input, context)
        if not session_id:
            return {"error": True, "message": "session_id is required"}
        payload: dict[str, Any] = {
            "session_id": session_id,
            "turn_limit": min(max(1, self._coerce_int(tool_input.get("turn_limit"), 8)), 200),
            "raw_history_limit": min(max(1, self._coerce_int(tool_input.get("raw_history_limit"), 12)), 200),
        }
        task_id = str(tool_input.get("task_id") or "").strip() or (context.task_id if context else None)
        if task_id:
            payload["task_id"] = task_id
        request_id = str(tool_input.get("request_id") or "").strip() or (context.request_id if context else None)
        if request_id:
            payload["request_id"] = request_id
        return await self._request_gateway_json(
            "POST",
            "/internal/session/revisit",
            json_body=payload,
        )

    # ── Create Reminder (Gateway Scheduler) ─────────────────────

    async def _create_reminder(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        label = str(tool_input.get("label") or "").strip()
        cron_expression = str(tool_input.get("cron_expression") or "").strip()
        prompt = str(tool_input.get("prompt") or "").strip()
        one_shot = self._coerce_bool(tool_input.get("one_shot"), default=True)
        if not label or not cron_expression or not prompt:
            return {"error": True, "message": "label, cron_expression, and prompt are required"}
        if not self.gateway_url:
            return {"error": True, "message": "Gateway scheduler is not configured."}
        request_body: dict[str, Any] = {
            "label": label,
            "cron_expression": cron_expression,
            "prompt": prompt,
            "one_shot": one_shot,
            "source": "orchestrator",
        }
        timezone_name = str(tool_input.get("timezone") or "").strip()
        if timezone_name:
            request_body["timezone"] = timezone_name
        delivery_target = str(tool_input.get("delivery_target") or "").strip()
        if delivery_target:
            request_body["delivery_target"] = delivery_target
        delivery_channel = str(tool_input.get("delivery_channel") or "").strip()
        if delivery_channel:
            request_body["delivery_channel"] = delivery_channel
        context_summary = str(tool_input.get("context_summary") or "").strip()
        if context_summary:
            request_body["context_summary"] = context_summary
        if context:
            if context.request_id:
                request_body["request_id"] = context.request_id
            if context.session_id:
                request_body["session_id"] = context.session_id
            if context.channel:
                request_body["channel"] = context.channel
        response = await self._request_gateway_json(
            "POST",
            "/internal/scheduler/crons",
            json_body=request_body,
        )
        return {
            "created": True,
            "cron_id": response.get("cron_id"),
            "label": label,
            "cron_expression": cron_expression,
            "one_shot": one_shot,
            "timezone": response.get("timezone"),
            "delivery_target": response.get("delivery_target"),
            "next_fire_at": response.get("next_fire_at"),
            "next_fire_local": response.get("next_fire_local"),
            "delivery_channel": response.get("delivery_channel"),
            "resolved_delivery_channel": response.get("resolved_delivery_channel"),
            "context_summary": response.get("context_summary"),
            "message": f"Reminder created: {label}",
        }

    # ── List Reminders ──────────────────────────────────────────

    async def _list_reminders(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        del tool_input, context
        if not self.gateway_url:
            return {"error": True, "message": "Gateway scheduler is not configured."}
        payload = await self._request_gateway_json("GET", "/internal/scheduler/crons")
        crons = payload.get("crons") or []
        return {"reminders": crons}

    # ── Delete Reminder ─────────────────────────────────────────

    async def _delete_reminder(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        del context
        cron_id = str(tool_input.get("cron_id") or "").strip()
        if not cron_id:
            return {"error": True, "message": "cron_id is required"}
        if not self.gateway_url:
            return {"error": True, "message": "Gateway scheduler is not configured."}
        await self._request_gateway_json("DELETE", f"/internal/scheduler/crons/{cron_id}")

        return {"deleted": True, "cron_id": cron_id, "message": "Reminder deleted."}

    # ── Internal helpers ────────────────────────────────────────

    async def _dispatch_specialist_agent(
        self,
        *,
        intent: str,
        payload: dict[str, Any],
        context: ToolExecutionContext | None,
        agent_id: str | None,
        wait_timeout_sec: float | None,
    ) -> dict[str, Any]:
        if self._agent_dispatcher is None:
            return {"error": True, "message": f"{intent} is not configured in this orchestrator runtime."}
        if context is None or context.parent_task is None:
            return {"error": True, "message": f"{intent} requires the active parent task context."}

        result = await self._agent_dispatcher(
            parent_task=context.parent_task,
            intent=intent,
            input_payload=payload,
            agent_id=agent_id,
            wait_timeout_sec=wait_timeout_sec,
        )
        if isinstance(result, TaskInProgress):
            return {
                "error": True,
                "in_progress": True,
                "task_id": result.task_id,
                "idempotency_key": result.idempotency_key,
                "check_after_sec": result.check_after_sec,
                "message": f"{intent} is still running in the specialist agent.",
                "delegation": {"intent": intent, "agent_id": agent_id},
            }

        if result.status != "completed":
            error = result.error
            return {
                "error": True,
                "code": error.code if error else "AGENT_FAILED",
                "retryable": error.retryable if error else False,
                "next_action": error.next_action if error else "escalate",
                "message": error.message if error else f"{intent} failed in the specialist agent.",
                "delegation": {"intent": intent, "agent_id": agent_id},
            }

        output = result.output if isinstance(result.output, dict) else {}
        response = dict(output)
        if result.artifacts and "artifacts" not in response:
            response["artifacts"] = [artifact.model_dump(mode="json") for artifact in result.artifacts]
        response.setdefault("delegation", {"intent": intent, "agent_id": agent_id})
        return response

    async def _request_gateway_json(
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
            headers=self._gateway_headers(),
        )
        if allow_404 and response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ToolHTTPError(
                status_code=response.status_code,
                message=f"Gateway API error (status={response.status_code}): {response.text[:300]}",
            )
        payload = self._response_json_as_object(response, service_name="gateway")
        return payload

    async def _request_cosmic_memory_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> dict[str, Any] | None:
        if not self.cosmic_memory_url:
            raise RuntimeError("Memory service is not configured.")
        response = await self._client.request(
            method,
            f"{self.cosmic_memory_url}{path}",
            json=json_body,
            params=params,
            headers=self._memory_headers(),
        )
        if allow_404 and response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ToolHTTPError(
                status_code=response.status_code,
                message=f"Memory API error (status={response.status_code}): {response.text[:300]}",
            )
        return self._response_json_as_object(response, service_name="cosmic-memory")

    def _response_json_as_object(self, response: httpx.Response, *, service_name: str) -> dict[str, Any]:
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"{service_name} returned a non-object payload")
        return payload

    def _coerce_session_id(
        self,
        tool_input: dict[str, Any],
        context: ToolExecutionContext | None,
    ) -> str | None:
        session_id = str(tool_input.get("session_id") or "").strip()
        if session_id:
            return session_id
        if context and context.session_id:
            return context.session_id
        return None

    def _coerce_task_id(
        self,
        tool_input: dict[str, Any],
        context: ToolExecutionContext | None,
    ) -> str | None:
        task_id = str(tool_input.get("task_id") or "").strip()
        if task_id:
            return task_id
        if context and context.task_id:
            return context.task_id
        return None

    def _normalize_memory_search_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = dict(payload)
        items = result.get("items")
        if not isinstance(items, list):
            items = result.get("results")
        normalized_items = [item for item in items or [] if isinstance(item, dict)]
        result["items"] = normalized_items
        result.setdefault("results", normalized_items)
        for key in ("entities", "relations", "episodes", "search_plan"):
            value = result.get(key)
            result[key] = value if isinstance(value, list) else []
        if not normalized_items and not any(result[key] for key in ("entities", "relations", "episodes")):
            result.setdefault("message", "No matching memories found.")
        return result

    def _normalize_memory_record_result(
        self,
        payload: dict[str, Any],
        *,
        requested_memory_id: str,
    ) -> dict[str, Any]:
        record = dict(payload)
        memory_id = str(record.get("memory_id") or requested_memory_id).strip() or requested_memory_id
        tags = record.get("tags")
        metadata = record.get("metadata")
        provenance = record.get("provenance")
        return {
            "found": True,
            "memory_id": memory_id,
            "kind": record.get("kind"),
            "title": record.get("title"),
            "content": record.get("content"),
            "tags": tags if isinstance(tags, list) else [],
            "metadata": metadata if isinstance(metadata, dict) else {},
            "provenance": provenance if isinstance(provenance, dict) else {},
            "status": record.get("status"),
            "version": record.get("version"),
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "supersedes": record.get("supersedes"),
            "superseded_by": record.get("superseded_by"),
            "record": record,
        }

    def _build_memory_metadata(self, context: ToolExecutionContext | None) -> dict[str, Any]:
        if context is None:
            return {}
        metadata = {
            "stored_by": "cosmic/orchestrator:1.0.0",
            "task_id": context.task_id,
            "request_id": context.request_id,
            "session_id": context.session_id,
            "channel": context.channel,
            "source": context.source,
            "source_id": context.source_id,
        }
        return self._clean_mapping(metadata)

    def _build_orchestrator_provenance(self, context: ToolExecutionContext | None) -> dict[str, Any]:
        provenance = {
            "source_kind": "orchestrator_tool",
            "created_by": "cosmic/orchestrator:1.0.0",
            "task_id": context.task_id if context else None,
            "request_id": context.request_id if context else None,
            "session_id": context.session_id if context else None,
            "channel": context.channel if context else None,
            "source": context.source if context else None,
            "source_id": context.source_id if context else None,
        }
        return self._clean_mapping(provenance)

    def _derive_memory_title(self, content: str) -> str:
        normalized = str(content or "").strip()
        if not normalized:
            return "Untitled memory"
        title = normalized.splitlines()[0].strip()
        if len(title) > 72:
            title = title[:69].rstrip() + "..."
        return title or "Untitled memory"

    def _normalize_memory_write_kind(self, kind: str) -> str | None:
        normalized = str(kind or "").strip().lower()
        if not normalized:
            return "agent_note"
        alias_map = {
            "agent_note": "agent_note",
            "note": "agent_note",
            "user_data": "user_data",
            "preference": "user_data",
            "fact": "user_data",
            "relationship": "user_data",
            "goal": "user_data",
            "event": "user_data",
        }
        return alias_map.get(normalized)

    def _normalize_string_list(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item.strip() for item in (str(entry or "") for entry in value) if item.strip()]

    def _coerce_int(self, value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _coerce_bool(self, value: Any, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        return bool(value)

    def _merge_dicts(self, base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
        merged = dict(base)
        for key, value in extra.items():
            if value is None:
                continue
            merged[str(key)] = value
        return self._clean_mapping(merged)

    def _clean_mapping(self, value: dict[str, Any]) -> dict[str, Any]:
        return {
            str(key): item
            for key, item in value.items()
            if item not in ("", None, [], {})
        }

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
