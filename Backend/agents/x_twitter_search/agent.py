from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError
try:
    from xai_sdk import Client
    try:
        from xai_sdk.chat import system as xai_system_message
        from xai_sdk.chat import user as xai_user_message
    except ImportError:  # pragma: no cover - helper surface varies by SDK build
        xai_system_message = None  # type: ignore[assignment]
        xai_user_message = None  # type: ignore[assignment]
    try:
        from xai_sdk.tools import x_search
    except ImportError:  # pragma: no cover - helper surface varies by SDK build
        x_search = None  # type: ignore[assignment]
    _XAI_SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised indirectly in local test environments
    Client = None  # type: ignore[assignment]
    xai_system_message = None  # type: ignore[assignment]
    xai_user_message = None  # type: ignore[assignment]
    x_search = None  # type: ignore[assignment]
    _XAI_SDK_AVAILABLE = False

from shared import (
    AgentError,
    AgentResult,
    ArtifactManifest,
    TaskEnvelope,
    begin_metered_call,
    build_model_key,
    build_usage_event,
    estimate_usage_cost_usd,
    post_usage_event,
)
from shared.agent_runtime import AgentRuntime
from shared.sqlite_client import connect_sync

from .config import AGENT_ROOT, BACKEND_ROOT, XTwitterSearchConfig

logger = logging.getLogger(__name__)

_RUNS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS x_search_session_runs (
    task_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    intent TEXT NOT NULL,
    query TEXT,
    summary TEXT NOT NULL,
    artifact_json TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_x_search_session_runs_session_created
ON x_search_session_runs (session_id, created_at DESC);
"""


class NotablePost(BaseModel):
    author_handle: str | None = None
    post_url: str | None = None
    posted_at: str | None = None
    excerpt: str = Field(min_length=1)
    why_it_matters: str = Field(min_length=1)


class XSearchStructuredResponse(BaseModel):
    summary: str = Field(min_length=1)
    key_findings: list[str] = Field(default_factory=list)
    notable_posts: list[NotablePost] = Field(default_factory=list)


class XTwitterSearchAgentError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        retryable: bool,
        next_action: str,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.next_action = next_action
        self.status_code = status_code


class XTwitterSearchAgent(AgentRuntime):
    SEARCH_INTENT = "x.search"
    RECALL_SESSION_INTENT = "x.recall_session"

    def __init__(
        self,
        *,
        redis_client,
        config: XTwitterSearchConfig | None = None,
        instance_id: str | None = None,
        agent_secret: str | None = None,
        registry_db_path: str | Path | None = None,
        http_client=None,
        agent_root: str | Path | None = None,
        artifacts_root: str | Path | None = None,
        store_root: str | Path | None = None,
        runtime_root: str | Path | None = None,
        xai_client_factory: Callable[[str], Any] | None = None,
        x_search_tool_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config or XTwitterSearchConfig.from_env()
        self.agent_root = (Path(agent_root).expanduser() if agent_root else AGENT_ROOT).resolve()
        self.prompts_root = self.agent_root / "prompts"
        self.skills_path = self.agent_root / "skills" / "SKILLS.md"
        self.store_root = (Path(store_root).expanduser() if store_root else self.agent_root / "store").resolve()
        self.runtime_root = (Path(runtime_root).expanduser() if runtime_root else self.agent_root / "runtime").resolve()
        self.data_root = self.store_root / "data"
        self.cache_root = self.runtime_root / "cache"
        self.logs_root = self.runtime_root / "logs"
        self.learnings_path = self.store_root / "learnings.md"
        self.session_db_path = self.data_root / "x_search_session_runs.db"
        self.artifacts_root = (
            Path(artifacts_root).expanduser() if artifacts_root else BACKEND_ROOT / "runs" / "artifacts"
        ).resolve()
        self._sdk_available = _XAI_SDK_AVAILABLE or xai_client_factory is not None
        self._xai_client_factory = xai_client_factory or self._default_xai_client_factory
        self._x_search_tool_factory = x_search_tool_factory or self._default_x_search_tool_factory

        super().__init__(
            agent_card_path=self.agent_root / "agent_card.yaml",
            redis_client=redis_client,
            instance_id=instance_id,
            agent_secret=agent_secret,
            registry_db_path=registry_db_path,
            gateway_url=self.config.gateway_url,
            gateway_internal_token=self.config.gateway_internal_token,
            http_client=http_client,
        )

    async def on_startup(self) -> None:
        if not self._sdk_available:
            raise RuntimeError("xai-sdk is not installed for x_twitter_search agent startup.")
        if not self.config.xai_api_key:
            raise RuntimeError("XAI_API_KEY is required for x_twitter_search agent startup.")
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.logs_root.mkdir(parents=True, exist_ok=True)
        self.artifacts_root.mkdir(parents=True, exist_ok=True)
        self.store_root.mkdir(parents=True, exist_ok=True)
        if not self.learnings_path.exists():
            self.learnings_path.write_text(
                "# X Search Agent Learnings\n\n"
                "- Keep raw provider output in artifacts, not inline responses.\n"
                "- Prefer compact briefings grounded by citations and notable posts.\n",
                encoding="utf-8",
            )
        self._initialize_store()

    async def execute(self, task: TaskEnvelope) -> AgentResult:
        try:
            if task.intent == self.SEARCH_INTENT:
                return await self._handle_search(task)
            if task.intent == self.RECALL_SESSION_INTENT:
                return await self._handle_recall_session(task)
            return self._result_error(
                code="INVALID_INPUT",
                message=f"Unsupported intent: {task.intent}",
                retryable=False,
                next_action="escalate",
            )
        except XTwitterSearchAgentError as exc:
            logger.warning(
                "x_search_agent.handled_error task_id=%s intent=%s code=%s status=%s message=%s",
                task.task_id,
                task.intent,
                exc.code,
                exc.status_code,
                exc.message,
            )
            return self._result_error(
                code=exc.code,
                message=exc.message,
                retryable=exc.retryable,
                next_action=exc.next_action,
            )
        except Exception as exc:
            logger.exception("x_search_agent.unhandled_error task_id=%s intent=%s", task.task_id, task.intent)
            return self._result_error(
                code="INTERNAL_ERROR",
                message=str(exc).strip()[:500] or "X search agent failed unexpectedly.",
                retryable=False,
                next_action="escalate",
            )

    async def _handle_search(self, task: TaskEnvelope) -> AgentResult:
        prompt_assets = self._load_prompt_assets()
        normalized_input = self._normalize_search_input(task.input)
        system_prompt = self._build_system_prompt(prompt_assets)
        user_prompt = self._build_user_prompt(
            normalized_input,
            session_entries=self._load_recent_session_context(task.session_id, exclude_task_id=task.task_id),
        )

        await self._emit_progress(task.task_id, f"Searching X for: {normalized_input['query']}")
        metered_call = begin_metered_call(prefix="call")
        started = time.perf_counter()
        response = None
        provider_payload: dict[str, Any] | None = None

        try:
            response, structured = await asyncio.wait_for(
                asyncio.to_thread(
                    self._run_x_search_sync,
                    system_prompt,
                    user_prompt,
                    normalized_input,
                ),
                timeout=self.config.x_search_request_timeout_sec,
            )
            provider_payload = self._serialize_provider_response(response)
        except TimeoutError as exc:
            await self._post_usage(
                metered_call=metered_call,
                task=task,
                response=None,
                raw_usage=None,
                success=False,
                error_code="TIMEOUT",
                metadata_json={"tool": "x_search", "query": normalized_input["query"]},
            )
            raise XTwitterSearchAgentError(
                code="TIMEOUT",
                message="xAI X search timed out before completing.",
                retryable=True,
                next_action="retry",
            ) from exc
        except Exception as exc:
            await self._post_usage(
                metered_call=metered_call,
                task=task,
                response=response,
                raw_usage=getattr(response, "usage", None) if response is not None else None,
                success=False,
                error_code="EXCEPTION",
                metadata_json={"tool": "x_search", "query": normalized_input["query"]},
            )
            mapped = self._map_provider_exception(exc)
            if mapped is not None:
                raise mapped from exc
            raise

        elapsed_ms = max(1, int((time.perf_counter() - started) * 1000))
        await self._post_usage(
            metered_call=metered_call,
            task=task,
            response=response,
            raw_usage=getattr(response, "usage", None),
            success=True,
            error_code=None,
            metadata_json={
                "tool": "x_search",
                "query": normalized_input["query"],
                "tool_calls": self._serialize_any(getattr(response, "tool_calls", None)),
                "server_side_tool_usage": self._serialize_any(getattr(response, "server_side_tool_usage", None)),
            },
        )

        citations = self._merge_citations(
            self._normalize_citations(getattr(response, "citations", None)),
            self._citations_from_notable_posts(structured.notable_posts),
        )
        provider_response_id = str(getattr(response, "id", "") or "").strip() or None
        tool_usage = {
            "server_side_tool_usage": self._serialize_any(getattr(response, "server_side_tool_usage", None)),
            "tool_calls": self._serialize_any(getattr(response, "tool_calls", None)),
            "latency_ms": elapsed_ms,
            "provider_response_id": provider_response_id,
        }
        artifact_manifests, artifact_refs = self._persist_search_artifacts(
            task=task,
            query=normalized_input["query"],
            normalized_output={
                "summary": structured.summary,
                "key_findings": structured.key_findings,
                "notable_posts": [item.model_dump(mode="json") for item in structured.notable_posts],
                "citations": citations,
                "filters": normalized_input,
                "tool_usage": tool_usage,
            },
            provider_payload=provider_payload or {},
        )

        message = f"Completed X search for: {normalized_input['query']}"
        output = {
            "response": message,
            "message": message,
            "query": normalized_input["query"],
            "summary": structured.summary,
            "key_findings": structured.key_findings[:8],
            "notable_posts": [item.model_dump(mode="json") for item in structured.notable_posts][: normalized_input["max_posts"]],
            "citations": citations,
            "filters": normalized_input,
            "tool_usage": tool_usage,
            "artifacts": artifact_refs,
        }
        details = {
            "elapsed_ms": elapsed_ms,
            "provider_response_id": provider_response_id,
            "filters": normalized_input,
            "citation_count": len(citations),
            "prompt_assets_loaded": sorted(key for key, value in prompt_assets.items() if value),
        }
        self._record_session_run(
            task=task,
            intent=task.intent,
            query=normalized_input["query"],
            summary=structured.summary,
            artifact_refs=artifact_refs,
            details=details,
        )
        logger.info(
            "x_search_agent.search_completed task_id=%s query=%s citations=%d elapsed_ms=%d",
            task.task_id,
            normalized_input["query"],
            len(citations),
            elapsed_ms,
        )
        return AgentResult(status="completed", output=output, artifacts=artifact_manifests)

    async def _handle_recall_session(self, task: TaskEnvelope) -> AgentResult:
        session_id = str(task.input.get("session_id") or "").strip()
        if not session_id:
            raise XTwitterSearchAgentError(
                code="INVALID_INPUT",
                message="session_id is required for x.recall_session.",
                retryable=False,
                next_action="revise_input",
            )
        limit = self._optional_int(task.input.get("limit"), minimum=1, maximum=50) or 10
        entries = self._load_session_entries(session_id=session_id, limit=limit)
        response = (
            f"Loaded {len(entries)} X search run{'s' if len(entries) != 1 else ''} from {session_id}."
            if entries else
            f"No X search runs were recorded for {session_id}."
        )
        return AgentResult(
            status="completed",
            output={"response": response, "session_id": session_id, "entries": entries},
            artifacts=[],
        )

    def _run_x_search_sync(
        self,
        system_prompt: str,
        user_prompt: str,
        normalized_input: dict[str, Any],
    ) -> tuple[Any, XSearchStructuredResponse]:
        client = self._xai_client_factory(self.config.xai_api_key)
        x_search_tool = self._x_search_tool_factory(
            **self._build_x_search_tool_kwargs(normalized_input)
        )
        chat = client.chat.create(
            model=self.config.x_search_model,
            response_format=XSearchStructuredResponse,
            max_tokens=self.config.x_search_max_output_tokens,
            tools=[x_search_tool],
            include=["inline_citations"],
        )
        self._append_chat_message(chat, role="system", content=system_prompt)
        self._append_chat_message(chat, role="user", content=user_prompt)
        response = chat.sample()
        raw_content = str(getattr(response, "content", "") or "").strip()
        if not raw_content:
            raise XTwitterSearchAgentError(
                code="INTERNAL_ERROR",
                message="xAI returned an empty X-search response.",
                retryable=False,
                next_action="escalate",
            )
        try:
            structured = XSearchStructuredResponse.model_validate_json(raw_content)
        except ValidationError as exc:
            raise XTwitterSearchAgentError(
                code="INTERNAL_ERROR",
                message="xAI returned an invalid structured X-search payload.",
                retryable=False,
                next_action="escalate",
            ) from exc
        return response, structured

    async def _emit_progress(self, task_id: str, message: str, **payload: Any) -> None:
        progress_payload = {"message": message}
        progress_payload.update(payload)
        await self.emit_event(task_id, "task.progress", progress_payload)

    async def _post_usage(
        self,
        *,
        metered_call,
        task: TaskEnvelope,
        response: Any,
        raw_usage: Any,
        success: bool,
        error_code: str | None,
        metadata_json: dict[str, Any],
    ) -> None:
        event = build_usage_event(
            metered_call=metered_call,
            source_component="agent",
            source_id=self.agent_id,
            task_id=task.task_id,
            parent_task_id=task.parent_task_id,
            session_id=task.session_id,
            route="specialist",
            operation="agent.x.search",
            model_key=build_model_key("xai", self.config.x_search_model),
            request_id=getattr(task, "request_id", None),
            provider_request_id=str(getattr(response, "id", "") or "").strip() or None,
            raw_usage=raw_usage,
            estimated_cost_usd=self._estimate_total_usage_cost_usd(response=response, raw_usage=raw_usage),
            success=success,
            error_code=error_code if not success else None,
            metadata_json=metadata_json,
        )
        try:
            await post_usage_event(
                client=self._http_client,
                gateway_url=self.gateway_url,
                internal_token=self.gateway_internal_token,
                event=event,
            )
        except Exception:
            logger.exception(
                "x_search_agent.usage_post_failed task_id=%s llm_call_id=%s",
                task.task_id,
                event.llm_call_id,
            )

    def _normalize_search_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("query") or "").strip()
        if len(query) < 3:
            raise XTwitterSearchAgentError(
                code="INVALID_INPUT",
                message="query must be at least 3 characters.",
                retryable=False,
                next_action="revise_input",
            )
        allowed_x_handles = self._normalize_handles(payload.get("allowed_x_handles"))
        excluded_x_handles = self._normalize_handles(payload.get("excluded_x_handles"))
        if allowed_x_handles and excluded_x_handles:
            raise XTwitterSearchAgentError(
                code="INVALID_INPUT",
                message="allowed_x_handles and excluded_x_handles cannot both be set.",
                retryable=False,
                next_action="revise_input",
            )
        from_date = self._normalize_optional_iso_datetime(payload.get("from_date"), field_name="from_date")
        to_date = self._normalize_optional_iso_datetime(payload.get("to_date"), field_name="to_date")
        if from_date and to_date and from_date > to_date:
            raise XTwitterSearchAgentError(
                code="INVALID_INPUT",
                message="from_date must be less than or equal to to_date.",
                retryable=False,
                next_action="revise_input",
            )
        max_posts = self._optional_int(
            payload.get("max_posts"),
            minimum=1,
            maximum=min(12, self.config.x_search_max_posts),
        ) or min(8, self.config.x_search_max_posts)
        return {
            "query": query,
            "analysis_goal": str(payload.get("analysis_goal") or "").strip() or None,
            "allowed_x_handles": allowed_x_handles,
            "excluded_x_handles": excluded_x_handles,
            "from_date": from_date,
            "to_date": to_date,
            "enable_image_understanding": self._coerce_bool(payload.get("enable_image_understanding"), default=False),
            "enable_video_understanding": self._coerce_bool(payload.get("enable_video_understanding"), default=False),
            "max_posts": max_posts,
        }

    def _build_system_prompt(self, prompt_assets: dict[str, str]) -> str:
        parts = [
            prompt_assets.get("system", ""),
            prompt_assets.get("policies", ""),
            prompt_assets.get("skills", ""),
            prompt_assets.get("learnings", ""),
            (
                "Return only valid JSON matching the response schema. "
                "Ground the answer in X search evidence. Prefer concise key findings and only include notable posts that materially support the result."
            ),
        ]
        return "\n\n".join(part for part in parts if part).strip()

    def _build_user_prompt(
        self,
        normalized_input: dict[str, Any],
        *,
        session_entries: list[dict[str, Any]] | None = None,
    ) -> str:
        request_payload = {
            "query": normalized_input["query"],
            "analysis_goal": normalized_input["analysis_goal"],
            "allowed_x_handles": normalized_input["allowed_x_handles"],
            "excluded_x_handles": normalized_input["excluded_x_handles"],
            "from_date": normalized_input["from_date"],
            "to_date": normalized_input["to_date"],
            "enable_image_understanding": normalized_input["enable_image_understanding"],
            "enable_video_understanding": normalized_input["enable_video_understanding"],
            "max_posts": normalized_input["max_posts"],
        }
        parts = [
            "Search X/Twitter globally and produce a deep, evidence-grounded briefing for this request. "
            "Prefer a compact synthesis over noisy raw search output.\n\n"
            f"{json.dumps(request_payload, ensure_ascii=False)}"
        ]
        if session_entries:
            parts.append(
                "Recent X-search session context (reuse only if relevant, but still perform fresh X search):\n\n"
                f"{json.dumps(session_entries, ensure_ascii=False)}"
            )
        return "\n\n".join(parts)

    def _persist_search_artifacts(
        self,
        *,
        task: TaskEnvelope,
        query: str,
        normalized_output: dict[str, Any],
        provider_payload: dict[str, Any],
    ) -> tuple[list[ArtifactManifest], list[dict[str, str]]]:
        manifests: list[ArtifactManifest] = []
        manifests.append(self._write_json_artifact(task=task, filename="x_search_response.json", payload=provider_payload))
        manifests.append(self._write_json_artifact(task=task, filename="x_search_report.json", payload=normalized_output))
        report_md = self._render_markdown_report(query=query, normalized_output=normalized_output)
        manifests.append(
            self._write_text_artifact(
                task=task,
                filename="x_search_report.md",
                content=report_md,
                mime="text/markdown",
            )
        )
        return manifests, [self._artifact_ref(item) for item in manifests]

    def _render_markdown_report(self, *, query: str, normalized_output: dict[str, Any]) -> str:
        lines = ["# X Search Report", "", f"Query: {query}", "", "## Summary", "", normalized_output["summary"], ""]
        findings = normalized_output.get("key_findings") or []
        if findings:
            lines.extend(["## Key Findings", ""])
            lines.extend(f"- {item}" for item in findings)
            lines.append("")
        posts = normalized_output.get("notable_posts") or []
        if posts:
            lines.extend(["## Notable Posts", ""])
            for post in posts:
                handle = post.get("author_handle") or "unknown"
                lines.append(f"- @{handle}: {post.get('excerpt') or ''}")
                why = str(post.get("why_it_matters") or "").strip()
                if why:
                    lines.append(f"  Why it matters: {why}")
            lines.append("")
        citations = normalized_output.get("citations") or []
        if citations:
            lines.extend(["## Citations", ""])
            for citation in citations:
                title = str(citation.get("title") or citation.get("url") or "Source").strip()
                url = str(citation.get("url") or "").strip()
                lines.append(f"- {title}: {url}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _normalize_citations(self, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        citations: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for item in value:
            if isinstance(item, str):
                url = item.strip()
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    citations.append({"title": "X Source", "url": url, "description": "Source from X search"})
                continue
            if isinstance(item, dict):
                url = str(item.get("url") or "").strip()
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                citations.append(
                    {
                        "title": str(item.get("title") or "X Source").strip() or "X Source",
                        "url": url,
                        "description": str(item.get("description") or "Source from X search").strip()
                        or "Source from X search",
                    }
                )
        return citations[:20]

    def _citations_from_notable_posts(self, posts: list[NotablePost]) -> list[dict[str, str]]:
        citations: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for post in posts:
            url = str(post.post_url or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            handle = str(post.author_handle or "").strip().lstrip("@")
            posted_at = str(post.posted_at or "").strip()
            title = f"@{handle} on X" if handle else "X Post"
            description_parts = []
            excerpt = str(post.excerpt or "").strip()
            if excerpt:
                description_parts.append(excerpt[:240])
            if posted_at:
                description_parts.append(f"Posted {posted_at}")
            citations.append(
                {
                    "title": title,
                    "url": url,
                    "description": " | ".join(description_parts) or "Source from X search",
                }
            )
        return citations[:20]

    def _merge_citations(
        self,
        primary: list[dict[str, str]],
        fallback: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        merged: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for item in [*primary, *fallback]:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            merged.append(item)
            if len(merged) >= 20:
                break
        return merged

    def _estimate_total_usage_cost_usd(self, *, response: Any, raw_usage: Any) -> float | None:
        model_key = build_model_key("xai", self.config.x_search_model)
        token_cost_usd = estimate_usage_cost_usd(model_key, raw_usage=raw_usage)
        tool_cost_usd = self._estimate_x_search_tool_cost_usd(response)
        if token_cost_usd is None and tool_cost_usd is None:
            return None
        return round(float(token_cost_usd or 0.0) + float(tool_cost_usd or 0.0), 10)

    def _estimate_x_search_tool_cost_usd(self, response: Any) -> float | None:
        tool_usage = self._serialize_any(getattr(response, "server_side_tool_usage", None))
        count = self._extract_x_search_tool_call_count(tool_usage)
        if count <= 0:
            return None
        return round((count / 1000.0) * 5.0, 10)

    def _extract_x_search_tool_call_count(self, value: Any) -> int:
        if isinstance(value, dict):
            total = 0
            for key, item in value.items():
                normalized_key = str(key or "").strip().lower()
                if "x_search" in normalized_key:
                    try:
                        total += max(0, int(float(item)))
                    except (TypeError, ValueError):
                        continue
            return total
        return 0

    def _serialize_provider_response(self, response: Any) -> dict[str, Any]:
        payload = self._serialize_any(response)
        return payload if isinstance(payload, dict) else {"response": payload}

    def _serialize_any(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(key): self._serialize_any(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._serialize_any(item) for item in value]
        if hasattr(value, "model_dump"):
            return self._serialize_any(value.model_dump(mode="json"))
        if hasattr(value, "dict") and callable(value.dict):
            return self._serialize_any(value.dict())
        if hasattr(value, "__dict__"):
            return self._serialize_any(vars(value))
        return str(value)

    def _record_session_run(
        self,
        *,
        task: TaskEnvelope,
        intent: str,
        query: str,
        summary: str,
        artifact_refs: list[dict[str, str]],
        details: dict[str, Any],
    ) -> None:
        session_id = str(task.session_id or "").strip() or "no_session"
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with connect_sync(self.session_db_path) as connection:
            connection.executescript(_RUNS_TABLE_SQL)
            connection.execute(
                """
                INSERT OR REPLACE INTO x_search_session_runs (
                    task_id,
                    session_id,
                    intent,
                    query,
                    summary,
                    artifact_json,
                    details_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    session_id,
                    intent,
                    query,
                    summary,
                    json.dumps(artifact_refs, ensure_ascii=False),
                    json.dumps(details, ensure_ascii=False),
                    created_at,
                ),
            )

    def _load_session_entries(self, *, session_id: str, limit: int) -> list[dict[str, Any]]:
        query = """
            SELECT task_id, intent, summary, query, artifact_json, created_at
            FROM x_search_session_runs
            WHERE session_id = ?
            ORDER BY created_at DESC, task_id DESC
            LIMIT ?
        """
        with connect_sync(self.session_db_path) as connection:
            rows = connection.execute(query, (session_id, limit)).fetchall()
        entries: list[dict[str, Any]] = []
        for row in rows:
            entries.append(
                {
                    "task_id": row["task_id"],
                    "intent": row["intent"],
                    "summary": row["summary"],
                    "query": row["query"],
                    "artifact_refs": self._json_loads_list(row["artifact_json"]),
                    "created_at": row["created_at"],
                }
            )
        return entries

    def _load_recent_session_context(
        self,
        session_id: str | None,
        *,
        exclude_task_id: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return []
        entries = self._load_session_entries(session_id=normalized_session_id, limit=max(limit + 1, 1))
        compact_entries: list[dict[str, Any]] = []
        for entry in entries:
            if exclude_task_id and str(entry.get("task_id") or "").strip() == exclude_task_id:
                continue
            compact_entries.append(
                {
                    "task_id": entry.get("task_id"),
                    "query": entry.get("query"),
                    "summary": entry.get("summary"),
                    "created_at": entry.get("created_at"),
                }
            )
            if len(compact_entries) >= limit:
                break
        return compact_entries

    def _initialize_store(self) -> None:
        with connect_sync(self.session_db_path) as connection:
            connection.executescript(_RUNS_TABLE_SQL)

    def _load_prompt_assets(self) -> dict[str, str]:
        return {
            "system": self._read_text_asset(self.prompts_root / "system.md"),
            "policies": self._read_text_asset(self.prompts_root / "policies.md"),
            "skills": self._read_text_asset(self.skills_path),
            "learnings": self._read_text_asset(self.learnings_path),
        }

    def _read_text_asset(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    def _write_json_artifact(
        self,
        *,
        task: TaskEnvelope,
        filename: str,
        payload: dict[str, Any],
    ) -> ArtifactManifest:
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        return self._write_text_artifact(
            task=task,
            filename=filename,
            content=content,
            mime="application/json",
        )

    def _write_text_artifact(
        self,
        *,
        task: TaskEnvelope,
        filename: str,
        content: str,
        mime: str,
    ) -> ArtifactManifest:
        task_dir = self.artifacts_root / task.task_id / "x_twitter_search"
        task_dir.mkdir(parents=True, exist_ok=True)
        path = task_dir / filename
        path.write_text(content, encoding="utf-8")
        try:
            artifact_path = path.relative_to(BACKEND_ROOT).as_posix()
        except ValueError:
            artifact_path = path.as_posix()
        return ArtifactManifest(
            artifact_id=f"art_{uuid4().hex[:12]}",
            task_id=task.task_id,
            mime=mime,
            sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            path=artifact_path,
            source_url=None,
            created_by_agent=self.agent_id,
        )

    def _artifact_ref(self, artifact: ArtifactManifest) -> dict[str, str]:
        return {
            "artifact_id": artifact.artifact_id,
            "path": artifact.path,
            "mime": artifact.mime,
        }

    def _normalize_handles(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        handles: list[str] = []
        seen: set[str] = set()
        for item in value:
            normalized = str(item or "").strip().lstrip("@")
            if not normalized:
                continue
            lowered = normalized.casefold()
            if lowered in seen:
                continue
            seen.add(lowered)
            handles.append(normalized)
        return handles[:10]

    def _normalize_optional_iso_datetime(self, value: Any, *, field_name: str) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            if len(text) == 10:
                try:
                    parsed = datetime.fromisoformat(f"{text}T00:00:00+00:00")
                except ValueError as exc:
                    raise XTwitterSearchAgentError(
                        code="INVALID_INPUT",
                        message=f"{field_name} must be a valid ISO-8601 datetime or date.",
                        retryable=False,
                        next_action="revise_input",
                    ) from exc
            else:
                raise XTwitterSearchAgentError(
                    code="INVALID_INPUT",
                    message=f"{field_name} must be a valid ISO-8601 datetime or date.",
                    retryable=False,
                    next_action="revise_input",
                )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _optional_int(
        self,
        value: Any,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int | None:
        if value in ("", None):
            return None
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise XTwitterSearchAgentError(
                code="INVALID_INPUT",
                message=f"Expected integer value, received {value!r}.",
                retryable=False,
                next_action="revise_input",
            ) from exc
        if minimum is not None and result < minimum:
            raise XTwitterSearchAgentError(
                code="INVALID_INPUT",
                message=f"Value must be >= {minimum}.",
                retryable=False,
                next_action="revise_input",
            )
        if maximum is not None and result > maximum:
            raise XTwitterSearchAgentError(
                code="INVALID_INPUT",
                message=f"Value must be <= {maximum}.",
                retryable=False,
                next_action="revise_input",
            )
        return result

    def _coerce_bool(self, value: Any, *, default: bool) -> bool:
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

    def _json_loads_list(self, value: str) -> list[dict[str, Any]]:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    def _result_error(
        self,
        *,
        code: str,
        message: str,
        retryable: bool,
        next_action: str,
    ) -> AgentResult:
        return AgentResult(
            status="failed",
            output={},
            artifacts=[],
            error=AgentError(
                code=code,
                retryable=retryable,
                message=message,
                next_action=next_action,
            ),
        )

    def _default_xai_client_factory(self, api_key: str) -> Any:
        if Client is None:
            raise RuntimeError("xai-sdk is not installed.")
        return Client(api_key=api_key)

    def _default_x_search_tool_factory(self, **kwargs: Any) -> Any:
        if x_search is None:
            return {"type": "x_search", **kwargs}
        return x_search(**kwargs)

    def _build_x_search_tool_kwargs(self, normalized_input: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if normalized_input.get("allowed_x_handles"):
            kwargs["allowed_x_handles"] = normalized_input["allowed_x_handles"]
        if normalized_input.get("excluded_x_handles"):
            kwargs["excluded_x_handles"] = normalized_input["excluded_x_handles"]
        from_date = self._parse_sdk_datetime(normalized_input.get("from_date"))
        if from_date is not None:
            kwargs["from_date"] = from_date
        to_date = self._parse_sdk_datetime(normalized_input.get("to_date"))
        if to_date is not None:
            kwargs["to_date"] = to_date
        if normalized_input.get("enable_image_understanding"):
            kwargs["enable_image_understanding"] = True
        if normalized_input.get("enable_video_understanding"):
            kwargs["enable_video_understanding"] = True
        return kwargs

    def _parse_sdk_datetime(self, value: Any) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _append_chat_message(self, chat: Any, *, role: str, content: str) -> None:
        helper_payload = None
        if role == "system" and xai_system_message is not None:
            helper_payload = xai_system_message(content)
        elif role == "user" and xai_user_message is not None:
            helper_payload = xai_user_message(content)
        for payload in (
            helper_payload,
            {"role": role, "content": content},
            content,
        ):
            if payload is None:
                continue
            try:
                chat.append(payload)
                return
            except Exception:
                continue
        raise RuntimeError("xAI SDK chat object rejected the message payload shape.")

    def _map_provider_exception(self, exc: Exception) -> XTwitterSearchAgentError | None:
        message = str(exc).strip() or exc.__class__.__name__
        lowered = message.casefold()
        if "429" in lowered or "rate limit" in lowered or "rate_limit" in lowered:
            return XTwitterSearchAgentError(
                code="RATE_LIMITED",
                message=message,
                retryable=True,
                next_action="retry",
            )
        if "401" in lowered or "403" in lowered or "unauthorized" in lowered or "invalid api key" in lowered:
            return XTwitterSearchAgentError(
                code="AUTH_ERROR",
                message=message,
                retryable=False,
                next_action="escalate",
            )
        if "timeout" in lowered or "timed out" in lowered:
            return XTwitterSearchAgentError(
                code="TIMEOUT",
                message=message,
                retryable=True,
                next_action="retry",
            )
        if any(token in lowered for token in ("connection", "network", "dns", "socket", "ssl")):
            return XTwitterSearchAgentError(
                code="NETWORK_ERROR",
                message=message,
                retryable=True,
                next_action="retry",
            )
        return None
