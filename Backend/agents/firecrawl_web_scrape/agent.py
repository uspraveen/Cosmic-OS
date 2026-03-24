from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from shared.agent_runtime import AgentRuntime
from shared.contracts import AgentError, AgentResult, ArtifactManifest, TaskEnvelope
from shared.sqlite_client import connect_sync

from .config import AGENT_ROOT, BACKEND_ROOT, FirecrawlWebScrapeConfig

logger = logging.getLogger(__name__)

_MAX_INLINE_MARKDOWN_CHARS = 4000
_MAX_INLINE_HTML_CHARS = 2000
_MAX_LIST_ITEMS = 25
_RUNS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS firecrawl_session_runs (
    task_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    intent TEXT NOT NULL,
    target_url TEXT,
    target_urls_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    artifact_json TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_firecrawl_session_runs_session_created
ON firecrawl_session_runs (session_id, created_at DESC);
"""


class FirecrawlAgentError(RuntimeError):
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


class FirecrawlWebScrapeAgent(AgentRuntime):
    SCRAPE_INTENT = "firecrawl.scrape"
    EXTRACT_INTENT = "firecrawl.extract"
    AGENT_INTENT = "firecrawl.agent"
    RECALL_SESSION_INTENT = "firecrawl.recall_session"
    SCRAPE_FORMATS = frozenset({"markdown", "html", "rawHtml", "links", "images", "screenshot"})
    SCRAPE_PROXY_VALUES = frozenset({"auto", "basic", "enhanced"})

    def __init__(
        self,
        *,
        redis_client,
        config: FirecrawlWebScrapeConfig | None = None,
        instance_id: str | None = None,
        agent_secret: str | None = None,
        registry_db_path: str | Path | None = None,
        http_client: httpx.AsyncClient | None = None,
        firecrawl_client: httpx.AsyncClient | None = None,
        agent_root: str | Path | None = None,
        artifacts_root: str | Path | None = None,
        store_root: str | Path | None = None,
        runtime_root: str | Path | None = None,
    ) -> None:
        self.config = config or FirecrawlWebScrapeConfig.from_env()
        self.agent_root = (Path(agent_root).expanduser() if agent_root else AGENT_ROOT).resolve()
        self.prompts_root = self.agent_root / "prompts"
        self.skills_path = self.agent_root / "skills" / "SKILLS.md"
        self.schemas_root = self.agent_root / "schemas" / "intents"
        self.store_root = (Path(store_root).expanduser() if store_root else self.agent_root / "store").resolve()
        self.runtime_root = (Path(runtime_root).expanduser() if runtime_root else self.agent_root / "runtime").resolve()
        self.data_root = self.store_root / "data"
        self.cache_root = self.runtime_root / "cache"
        self.logs_root = self.runtime_root / "logs"
        self.learnings_path = self.store_root / "learnings.md"
        self.session_db_path = self.data_root / "firecrawl_session_runs.db"
        self.artifacts_root = (
            Path(artifacts_root).expanduser() if artifacts_root else BACKEND_ROOT / "runs" / "artifacts"
        ).resolve()

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

        if firecrawl_client is not None:
            self._firecrawl_client = firecrawl_client
            self._owns_firecrawl_client = False
        elif http_client is not None:
            self._firecrawl_client = http_client
            self._owns_firecrawl_client = False
        else:
            timeout = httpx.Timeout(
                self.config.firecrawl_request_timeout_sec,
                connect=min(self.config.firecrawl_request_timeout_sec, 20.0),
            )
            self._firecrawl_client = httpx.AsyncClient(timeout=timeout, http2=True)
            self._owns_firecrawl_client = True

    async def on_startup(self) -> None:
        if not self.config.firecrawl_api_key:
            raise RuntimeError("FIRECRAWL_API_KEY is required for firecrawl_web_scrape agent startup.")
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.logs_root.mkdir(parents=True, exist_ok=True)
        self.artifacts_root.mkdir(parents=True, exist_ok=True)
        self.store_root.mkdir(parents=True, exist_ok=True)
        if not self.learnings_path.exists():
            self.learnings_path.write_text(
                "# Firecrawl Agent Learnings\n\n"
                "- Keep large scrape bodies in artifacts, not inline outputs.\n"
                "- Prefer compact summaries plus artifact references.\n",
                encoding="utf-8",
            )
        self._initialize_store()

    async def stop(self) -> None:
        try:
            if self._owns_firecrawl_client:
                await self._firecrawl_client.aclose()
        finally:
            await super().stop()

    async def execute(self, task: TaskEnvelope) -> AgentResult:
        try:
            if task.intent == self.SCRAPE_INTENT:
                return await self._handle_scrape(task)
            if task.intent == self.EXTRACT_INTENT:
                return await self._handle_extract(task)
            if task.intent == self.AGENT_INTENT:
                return await self._handle_agent(task)
            if task.intent == self.RECALL_SESSION_INTENT:
                return await self._handle_recall_session(task)
            return self._result_error(
                code="INVALID_INPUT",
                message=f"Unsupported intent: {task.intent}",
                retryable=False,
                next_action="escalate",
            )
        except FirecrawlAgentError as exc:
            logger.warning(
                "firecrawl_agent.handled_error task_id=%s intent=%s code=%s status=%s message=%s",
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
            logger.exception("firecrawl_agent.unhandled_error task_id=%s intent=%s", task.task_id, task.intent)
            return self._result_error(
                code="INTERNAL_ERROR",
                message=str(exc).strip()[:500] or "Firecrawl agent failed unexpectedly.",
                retryable=False,
                next_action="escalate",
            )

    async def _handle_scrape(self, task: TaskEnvelope) -> AgentResult:
        prompt_assets = self._load_prompt_assets()
        url = self._require_url(task.input.get("url"), field_name="url")
        formats = self._normalize_scrape_formats(task.input.get("formats"))
        payload: dict[str, Any] = {
            "url": url,
            "formats": formats,
            "onlyMainContent": self._coerce_bool(task.input.get("only_main_content"), default=True),
            "mobile": self._coerce_bool(task.input.get("mobile"), default=False),
        }
        wait_for_ms = self._optional_int(task.input.get("wait_for_ms"), minimum=0, maximum=120000)
        timeout_ms = self._optional_int(task.input.get("timeout_ms"), minimum=1000, maximum=180000)
        max_age_ms = self._optional_int(task.input.get("max_age_ms"), minimum=0)
        include_tags = self._normalize_string_list(task.input.get("include_tags"))
        exclude_tags = self._normalize_string_list(task.input.get("exclude_tags"))
        proxy = str(task.input.get("proxy") or "").strip()
        if wait_for_ms is not None:
            payload["waitFor"] = wait_for_ms
        if timeout_ms is not None:
            payload["timeout"] = timeout_ms
        if max_age_ms is not None:
            payload["maxAge"] = max_age_ms
        if include_tags:
            payload["includeTags"] = include_tags
        if exclude_tags:
            payload["excludeTags"] = exclude_tags
        if proxy:
            if proxy not in self.SCRAPE_PROXY_VALUES:
                raise FirecrawlAgentError(
                    code="INVALID_INPUT",
                    message="proxy must be one of: auto, basic, enhanced.",
                    retryable=False,
                    next_action="revise_input",
                )
            payload["proxy"] = proxy

        await self._emit_progress(task.task_id, f"Scraping {url} via Firecrawl.")
        started = time.perf_counter()
        response_payload = await self._firecrawl_request("POST", "/v2/scrape", json_body=payload)
        elapsed_ms = max(1, int((time.perf_counter() - started) * 1000))
        data = response_payload.get("data")
        if not isinstance(data, dict):
            raise FirecrawlAgentError(
                code="INTERNAL_ERROR",
                message="Firecrawl scrape response did not include a data object.",
                retryable=False,
                next_action="escalate",
            )

        artifact_manifests, artifact_refs = await self._persist_scrape_artifacts(
            task=task,
            source_url=url,
            response_payload=response_payload,
            data=data,
        )
        normalized_output = self._normalize_scrape_output(data)
        available_formats = normalized_output["available_formats"]
        metadata = normalized_output["metadata"]
        title = str(metadata.get("title") or "").strip() or None
        message = (
            f"Scraped {url} and captured {', '.join(available_formats)}."
            if available_formats else
            f"Scraped {url}."
        )
        output = {
            "response": message,
            "message": message,
            "url": url,
            "title": title,
            "available_formats": available_formats,
            "metadata": metadata,
            "data": normalized_output["data"],
            "artifacts": artifact_refs,
        }
        details = {
            "message": message,
            "elapsed_ms": elapsed_ms,
            "metadata": metadata,
            "available_formats": available_formats,
            "prompt_assets_loaded": sorted(key for key, value in prompt_assets.items() if value),
        }
        self._record_session_run(
            task=task,
            intent=task.intent,
            target_urls=[url],
            summary=message,
            artifact_refs=artifact_refs,
            details=details,
        )
        logger.info(
            "firecrawl_agent.scrape_completed task_id=%s url=%s formats=%s elapsed_ms=%d",
            task.task_id,
            url,
            ",".join(available_formats),
            elapsed_ms,
        )
        return AgentResult(status="completed", output=output, artifacts=artifact_manifests)

    async def _handle_extract(self, task: TaskEnvelope) -> AgentResult:
        prompt_assets = self._load_prompt_assets()
        urls = self._normalize_url_list(task.input.get("urls"))
        if not urls:
            raise FirecrawlAgentError(
                code="INVALID_INPUT",
                message="urls must contain at least one valid http(s) URL.",
                retryable=False,
                next_action="revise_input",
            )
        prompt = str(task.input.get("prompt") or "").strip()
        if len(prompt) < 5:
            raise FirecrawlAgentError(
                code="INVALID_INPUT",
                message="prompt must be at least 5 characters.",
                retryable=False,
                next_action="revise_input",
            )
        schema = task.input.get("schema") if isinstance(task.input.get("schema"), dict) else None
        scrape_options: dict[str, Any] = {
            "formats": ["markdown"],
            "onlyMainContent": self._coerce_bool(task.input.get("only_main_content"), default=True),
        }
        wait_for_ms = self._optional_int(task.input.get("wait_for_ms"), minimum=0, maximum=120000)
        timeout_ms = self._optional_int(task.input.get("timeout_ms"), minimum=1000, maximum=180000)
        max_age_ms = self._optional_int(task.input.get("max_age_ms"), minimum=0)
        if wait_for_ms is not None:
            scrape_options["waitFor"] = wait_for_ms
        if timeout_ms is not None:
            scrape_options["timeout"] = timeout_ms
        if max_age_ms is not None:
            scrape_options["maxAge"] = max_age_ms

        payload: dict[str, Any] = {
            "urls": urls,
            "prompt": prompt,
            "enableWebSearch": self._coerce_bool(task.input.get("enable_web_search"), default=False),
            "showSources": self._coerce_bool(task.input.get("show_sources"), default=False),
            "scrapeOptions": scrape_options,
        }
        if schema:
            payload["schema"] = schema

        await self._emit_progress(
            task.task_id,
            f"Submitting Firecrawl extraction for {len(urls)} page{'s' if len(urls) != 1 else ''}.",
        )
        started = time.perf_counter()
        submitted = await self._firecrawl_request("POST", "/v2/extract", json_body=payload)
        job_id = str(submitted.get("id") or "").strip()
        if not job_id:
            raise FirecrawlAgentError(
                code="INTERNAL_ERROR",
                message="Firecrawl extract response did not include a job ID.",
                retryable=False,
                next_action="escalate",
            )

        invalid_urls = self._normalize_string_list(submitted.get("invalidURLs") or submitted.get("invalid_urls"))
        deadline = time.monotonic() + max(self.config.firecrawl_extract_max_wait_sec, 15.0)
        latest_payload = submitted
        status = str(submitted.get("status") or "processing").strip().lower() or "processing"

        while status not in {"completed", "failed", "cancelled", "canceled"}:
            if time.monotonic() >= deadline:
                raise FirecrawlAgentError(
                    code="TIMEOUT",
                    message=f"Firecrawl extract job {job_id} did not finish before the configured timeout.",
                    retryable=True,
                    next_action="retry",
                )
            await self._emit_progress(task.task_id, f"Waiting on Firecrawl extract job {job_id} ({status}).")
            await asyncio.sleep(self.config.firecrawl_extract_poll_interval_sec)
            latest_payload = await self._firecrawl_request("GET", f"/v2/extract/{job_id}")
            status = str(latest_payload.get("status") or status).strip().lower() or status

        elapsed_ms = max(1, int((time.perf_counter() - started) * 1000))
        if status in {"failed", "cancelled", "canceled"}:
            message = str(latest_payload.get("error") or latest_payload.get("message") or "").strip()
            if not message:
                message = f"Firecrawl extract job {job_id} ended with status={status}."
            raise FirecrawlAgentError(
                code="INTERNAL_ERROR" if status == "failed" else "TIMEOUT",
                message=message,
                retryable=(status != "failed"),
                next_action="retry" if status != "failed" else "escalate",
            )

        extracted = latest_payload.get("data")
        sources = latest_payload.get("sources") if isinstance(latest_payload.get("sources"), list) else []
        artifact_manifests, artifact_refs = await self._persist_extract_artifacts(
            task=task,
            source_urls=urls,
            submitted_payload=submitted,
            final_payload=latest_payload,
            extracted_payload=extracted,
        )
        normalized_data = self._normalize_extract_data(extracted)
        message = f"Firecrawl extracted structured data from {len(urls)} page{'s' if len(urls) != 1 else ''}."
        output = {
            "response": message,
            "message": message,
            "job_id": job_id,
            "status": status,
            "prompt": prompt,
            "urls": urls,
            "data": normalized_data,
            "sources": sources,
            "invalid_urls": invalid_urls,
            "artifacts": artifact_refs,
        }
        details = {
            "job_id": job_id,
            "status": status,
            "elapsed_ms": elapsed_ms,
            "invalid_urls": invalid_urls,
            "prompt_assets_loaded": sorted(key for key, value in prompt_assets.items() if value),
        }
        self._record_session_run(
            task=task,
            intent=task.intent,
            target_urls=urls,
            summary=message,
            artifact_refs=artifact_refs,
            details=details,
        )
        logger.info(
            "firecrawl_agent.extract_completed task_id=%s job_id=%s urls=%d elapsed_ms=%d",
            task.task_id,
            job_id,
            len(urls),
            elapsed_ms,
        )
        return AgentResult(status="completed", output=output, artifacts=artifact_manifests)

    async def _handle_recall_session(self, task: TaskEnvelope) -> AgentResult:
        session_id = str(task.input.get("session_id") or "").strip()
        if not session_id:
            raise FirecrawlAgentError(
                code="INVALID_INPUT",
                message="session_id is required for firecrawl.recall_session.",
                retryable=False,
                next_action="revise_input",
            )
        limit = self._optional_int(task.input.get("limit"), minimum=1, maximum=50) or 10
        entries = self._load_session_entries(session_id=session_id, limit=limit)
        response = (
            f"Loaded {len(entries)} Firecrawl run{'s' if len(entries) != 1 else ''} from {session_id}."
            if entries else
            f"No Firecrawl runs were recorded for {session_id}."
        )
        return AgentResult(
            status="completed",
            output={
                "response": response,
                "session_id": session_id,
                "entries": entries,
            },
            artifacts=[],
        )

    async def _handle_agent(self, task: TaskEnvelope) -> AgentResult:
        prompt_assets = self._load_prompt_assets()
        prompt = str(task.input.get("prompt") or "").strip()
        if len(prompt) < 10:
            raise FirecrawlAgentError(
                code="INVALID_INPUT",
                message="prompt must be at least 10 characters for the Firecrawl agent.",
                retryable=False,
                next_action="revise_input",
            )
        urls = self._normalize_url_list(task.input.get("urls")) if isinstance(task.input.get("urls"), list) else []
        schema = task.input.get("schema") if isinstance(task.input.get("schema"), dict) else None

        payload: dict[str, Any] = {"prompt": prompt}
        if urls:
            payload["urls"] = urls
        if schema:
            payload["schema"] = schema

        url_desc = f" focused on {len(urls)} seed URL{'s' if len(urls) != 1 else ''}" if urls else ""
        await self._emit_progress(
            task.task_id,
            f"Submitting Firecrawl autonomous agent job{url_desc}.",
        )
        started = time.perf_counter()
        submitted = await self._firecrawl_request("POST", "/v2/agent", json_body=payload)
        job_id = str(submitted.get("id") or "").strip()
        if not job_id:
            raise FirecrawlAgentError(
                code="INTERNAL_ERROR",
                message="Firecrawl agent response did not include a job ID.",
                retryable=False,
                next_action="escalate",
            )

        deadline = time.monotonic() + max(self.config.firecrawl_agent_max_wait_sec, 30.0)
        latest_payload = submitted
        status = str(submitted.get("status") or "processing").strip().lower() or "processing"

        while status not in {"completed", "failed", "cancelled", "canceled"}:
            if time.monotonic() >= deadline:
                raise FirecrawlAgentError(
                    code="TIMEOUT",
                    message=f"Firecrawl agent job {job_id} did not finish before the configured timeout.",
                    retryable=True,
                    next_action="retry",
                )
            await self._emit_progress(task.task_id, f"Waiting on Firecrawl agent job {job_id} ({status}).")
            await asyncio.sleep(self.config.firecrawl_agent_poll_interval_sec)
            latest_payload = await self._firecrawl_request("GET", f"/v2/agent/{job_id}")
            status = str(latest_payload.get("status") or status).strip().lower() or status

        elapsed_ms = max(1, int((time.perf_counter() - started) * 1000))
        if status in {"failed", "cancelled", "canceled"}:
            message = str(latest_payload.get("error") or latest_payload.get("message") or "").strip()
            if not message:
                message = f"Firecrawl agent job {job_id} ended with status={status}."
            raise FirecrawlAgentError(
                code="INTERNAL_ERROR" if status == "failed" else "TIMEOUT",
                message=message,
                retryable=(status != "failed"),
                next_action="retry" if status != "failed" else "escalate",
            )

        agent_data = latest_payload.get("data")
        sources = latest_payload.get("sources") if isinstance(latest_payload.get("sources"), list) else []
        artifact_manifests, artifact_refs = await self._persist_agent_artifacts(
            task=task,
            source_urls=urls,
            submitted_payload=submitted,
            final_payload=latest_payload,
            agent_data=agent_data,
        )
        normalized_data = self._normalize_extract_data(agent_data)
        message = (
            f"Firecrawl agent completed autonomous extraction{url_desc}."
            if not urls else
            f"Firecrawl agent extracted data from {len(urls)} seed page{'s' if len(urls) != 1 else ''}."
        )
        output = {
            "response": message,
            "message": message,
            "job_id": job_id,
            "status": status,
            "prompt": prompt,
            "urls": urls,
            "data": normalized_data,
            "sources": sources,
            "artifacts": artifact_refs,
        }
        details = {
            "job_id": job_id,
            "status": status,
            "elapsed_ms": elapsed_ms,
            "prompt_assets_loaded": sorted(key for key, value in prompt_assets.items() if value),
        }
        self._record_session_run(
            task=task,
            intent=task.intent,
            target_urls=urls or [],
            summary=message,
            artifact_refs=artifact_refs,
            details=details,
        )
        logger.info(
            "firecrawl_agent.agent_completed task_id=%s job_id=%s urls=%d elapsed_ms=%d",
            task.task_id,
            job_id,
            len(urls),
            elapsed_ms,
        )
        return AgentResult(status="completed", output=output, artifacts=artifact_manifests)

    async def _persist_agent_artifacts(
        self,
        *,
        task: TaskEnvelope,
        source_urls: list[str],
        submitted_payload: dict[str, Any],
        final_payload: dict[str, Any],
        agent_data: Any,
    ) -> tuple[list[ArtifactManifest], list[dict[str, str]]]:
        manifests: list[ArtifactManifest] = []
        source_url = source_urls[0] if len(source_urls) == 1 else None
        manifests.append(
            self._write_json_artifact(
                task=task,
                filename="agent_submitted.json",
                payload=submitted_payload,
                source_url=source_url,
            )
        )
        manifests.append(
            self._write_json_artifact(
                task=task,
                filename="agent_result.json",
                payload=final_payload,
                source_url=source_url,
            )
        )
        manifests.append(
            self._write_json_artifact(
                task=task,
                filename="agent_data.json",
                payload={"data": agent_data},
                source_url=source_url,
            )
        )
        sources = final_payload.get("sources")
        if isinstance(sources, list) and sources:
            manifests.append(
                self._write_json_artifact(
                    task=task,
                    filename="agent_sources.json",
                    payload={"sources": sources},
                    source_url=source_url,
                )
            )
        return manifests, [self._artifact_ref(item) for item in manifests]

    async def _emit_progress(self, task_id: str, message: str, **payload: Any) -> None:
        progress_payload = {"message": message}
        progress_payload.update(payload)
        await self.emit_event(task_id, "task.progress", progress_payload)

    async def _firecrawl_request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.config.firecrawl_api_base_url.rstrip('/')}{path}"
        try:
            response = await self._firecrawl_client.request(
                method,
                url,
                json=json_body,
                headers={
                    "Authorization": f"Bearer {self.config.firecrawl_api_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.TimeoutException as exc:
            raise FirecrawlAgentError(
                code="TIMEOUT",
                message=f"Firecrawl request timed out for {path}.",
                retryable=True,
                next_action="retry",
            ) from exc
        except httpx.HTTPError as exc:
            raise FirecrawlAgentError(
                code="NETWORK_ERROR",
                message=f"Firecrawl request failed for {path}: {exc}",
                retryable=True,
                next_action="retry",
            ) from exc

        if response.status_code >= 400:
            raise self._map_firecrawl_http_error(response)
        payload = self._response_json(response)
        success = payload.get("success")
        if success is False:
            message = str(payload.get("error") or payload.get("message") or "Firecrawl returned success=false.").strip()
            raise FirecrawlAgentError(
                code="INTERNAL_ERROR",
                message=message,
                retryable=False,
                next_action="escalate",
                status_code=response.status_code,
            )
        return payload

    def _map_firecrawl_http_error(self, response: httpx.Response) -> FirecrawlAgentError:
        message = self._extract_error_message(response)
        status = response.status_code
        if status in {400, 404, 422}:
            return FirecrawlAgentError(
                code="INVALID_INPUT",
                message=message,
                retryable=False,
                next_action="revise_input",
                status_code=status,
            )
        if status in {401, 402, 403}:
            return FirecrawlAgentError(
                code="AUTH_ERROR",
                message=message,
                retryable=False,
                next_action="escalate",
                status_code=status,
            )
        if status == 429:
            return FirecrawlAgentError(
                code="RATE_LIMITED",
                message=message,
                retryable=True,
                next_action="retry",
                status_code=status,
            )
        if status in {408, 504}:
            return FirecrawlAgentError(
                code="TIMEOUT",
                message=message,
                retryable=True,
                next_action="retry",
                status_code=status,
            )
        return FirecrawlAgentError(
            code="NETWORK_ERROR" if status >= 500 else "INTERNAL_ERROR",
            message=message,
            retryable=status >= 500,
            next_action="retry" if status >= 500 else "escalate",
            status_code=status,
        )

    def _extract_error_message(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return response.text.strip()[:500] or f"Firecrawl request failed with status={response.status_code}."
        if isinstance(payload, dict):
            for key in ("error", "message", "detail"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:500]
        return f"Firecrawl request failed with status={response.status_code}."

    def _response_json(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise FirecrawlAgentError(
                code="INTERNAL_ERROR",
                message="Firecrawl returned invalid JSON.",
                retryable=False,
                next_action="escalate",
                status_code=response.status_code,
            ) from exc
        if not isinstance(payload, dict):
            raise FirecrawlAgentError(
                code="INTERNAL_ERROR",
                message="Firecrawl returned a non-object payload.",
                retryable=False,
                next_action="escalate",
                status_code=response.status_code,
            )
        return payload

    async def _persist_scrape_artifacts(
        self,
        *,
        task: TaskEnvelope,
        source_url: str,
        response_payload: dict[str, Any],
        data: dict[str, Any],
    ) -> tuple[list[ArtifactManifest], list[dict[str, str]]]:
        manifests: list[ArtifactManifest] = []
        manifests.append(
            self._write_json_artifact(
                task=task,
                filename="scrape_response.json",
                payload=response_payload,
                source_url=source_url,
            )
        )

        markdown = data.get("markdown")
        if isinstance(markdown, str) and markdown.strip():
            manifests.append(
                self._write_text_artifact(
                    task=task,
                    filename="page.md",
                    content=markdown,
                    mime="text/markdown",
                    source_url=source_url,
                )
            )
        html = data.get("html")
        if isinstance(html, str) and html.strip():
            manifests.append(
                self._write_text_artifact(
                    task=task,
                    filename="page.html",
                    content=html,
                    mime="text/html",
                    source_url=source_url,
                )
            )
        raw_html = data.get("rawHtml")
        if isinstance(raw_html, str) and raw_html.strip():
            manifests.append(
                self._write_text_artifact(
                    task=task,
                    filename="page.raw.html",
                    content=raw_html,
                    mime="text/html",
                    source_url=source_url,
                )
            )
        links = data.get("links")
        if isinstance(links, list) and links:
            manifests.append(
                self._write_json_artifact(
                    task=task,
                    filename="links.json",
                    payload={"links": links},
                    source_url=source_url,
                )
            )
        images = data.get("images")
        if isinstance(images, list) and images:
            manifests.append(
                self._write_json_artifact(
                    task=task,
                    filename="images.json",
                    payload={"images": images},
                    source_url=source_url,
                )
            )
        screenshot = data.get("screenshot")
        if screenshot not in (None, "", [], {}):
            manifests.append(
                self._write_json_artifact(
                    task=task,
                    filename="screenshot.json",
                    payload={"screenshot": screenshot},
                    source_url=source_url,
                )
            )
        return manifests, [self._artifact_ref(item) for item in manifests]

    async def _persist_extract_artifacts(
        self,
        *,
        task: TaskEnvelope,
        source_urls: list[str],
        submitted_payload: dict[str, Any],
        final_payload: dict[str, Any],
        extracted_payload: Any,
    ) -> tuple[list[ArtifactManifest], list[dict[str, str]]]:
        manifests: list[ArtifactManifest] = []
        source_url = source_urls[0] if len(source_urls) == 1 else None
        manifests.append(
            self._write_json_artifact(
                task=task,
                filename="extract_submitted.json",
                payload=submitted_payload,
                source_url=source_url,
            )
        )
        manifests.append(
            self._write_json_artifact(
                task=task,
                filename="extract_result.json",
                payload=final_payload,
                source_url=source_url,
            )
        )
        manifests.append(
            self._write_json_artifact(
                task=task,
                filename="extract_data.json",
                payload={"data": extracted_payload},
                source_url=source_url,
            )
        )
        sources = final_payload.get("sources")
        if isinstance(sources, list) and sources:
            manifests.append(
                self._write_json_artifact(
                    task=task,
                    filename="extract_sources.json",
                    payload={"sources": sources},
                    source_url=source_url,
                )
            )
        return manifests, [self._artifact_ref(item) for item in manifests]

    def _normalize_scrape_output(self, data: dict[str, Any]) -> dict[str, Any]:
        output_data: dict[str, Any] = {}
        available_formats: list[str] = []

        markdown = data.get("markdown")
        if isinstance(markdown, str) and markdown.strip():
            available_formats.append("markdown")
            output_data["markdown_excerpt"] = self._clip_text(markdown, limit=_MAX_INLINE_MARKDOWN_CHARS)

        html = data.get("html")
        if isinstance(html, str) and html.strip():
            available_formats.append("html")
            output_data["html_excerpt"] = self._clip_text(html, limit=_MAX_INLINE_HTML_CHARS)

        raw_html = data.get("rawHtml")
        if isinstance(raw_html, str) and raw_html.strip():
            available_formats.append("rawHtml")
            output_data["raw_html_excerpt"] = self._clip_text(raw_html, limit=_MAX_INLINE_HTML_CHARS)

        links = data.get("links")
        if isinstance(links, list):
            available_formats.append("links")
            output_data["links"] = links[:_MAX_LIST_ITEMS]
            output_data["links_count"] = len(links)

        images = data.get("images")
        if isinstance(images, list):
            available_formats.append("images")
            output_data["images"] = images[:_MAX_LIST_ITEMS]
            output_data["images_count"] = len(images)

        screenshot = data.get("screenshot")
        if screenshot not in (None, "", [], {}):
            available_formats.append("screenshot")
            output_data["screenshot"] = screenshot

        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        return {
            "available_formats": self._dedupe_preserve_order(available_formats),
            "metadata": metadata,
            "data": output_data,
        }

    def _normalize_extract_data(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, list):
            return {"items": payload}
        if payload is None:
            return {}
        return {"value": payload}

    def _record_session_run(
        self,
        *,
        task: TaskEnvelope,
        intent: str,
        target_urls: list[str],
        summary: str,
        artifact_refs: list[dict[str, str]],
        details: dict[str, Any],
    ) -> None:
        session_id = str(task.session_id or "").strip() or "no_session"
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        target_url = target_urls[0] if len(target_urls) == 1 else None
        with connect_sync(self.session_db_path) as connection:
            connection.executescript(_RUNS_TABLE_SQL)
            connection.execute(
                """
                INSERT OR REPLACE INTO firecrawl_session_runs (
                    task_id,
                    session_id,
                    intent,
                    target_url,
                    target_urls_json,
                    summary,
                    artifact_json,
                    details_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    session_id,
                    intent,
                    target_url,
                    json.dumps(target_urls, ensure_ascii=False),
                    summary,
                    json.dumps(artifact_refs, ensure_ascii=False),
                    json.dumps(details, ensure_ascii=False),
                    created_at,
                ),
            )

    def _load_session_entries(self, *, session_id: str, limit: int) -> list[dict[str, Any]]:
        query = """
            SELECT task_id, intent, summary, target_url, artifact_json, created_at
            FROM firecrawl_session_runs
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
                    "target_url": row["target_url"],
                    "artifact_refs": self._json_loads_list(row["artifact_json"]),
                    "created_at": row["created_at"],
                }
            )
        return entries

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
        source_url: str | None,
    ) -> ArtifactManifest:
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        return self._write_text_artifact(
            task=task,
            filename=filename,
            content=content,
            mime="application/json",
            source_url=source_url,
        )

    def _write_text_artifact(
        self,
        *,
        task: TaskEnvelope,
        filename: str,
        content: str,
        mime: str,
        source_url: str | None,
    ) -> ArtifactManifest:
        task_dir = self.artifacts_root / task.task_id / "firecrawl_web_scrape"
        task_dir.mkdir(parents=True, exist_ok=True)
        path = task_dir / filename
        path.write_text(content, encoding="utf-8")
        relative_path = path.relative_to(BACKEND_ROOT).as_posix()
        return ArtifactManifest(
            artifact_id=f"art_{uuid4().hex[:12]}",
            task_id=task.task_id,
            mime=mime,
            sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            path=relative_path,
            source_url=source_url,
            created_by_agent=self.agent_id,
            audience="supporting",
        )

    def _artifact_ref(self, artifact: ArtifactManifest) -> dict[str, str]:
        return {
            "artifact_id": artifact.artifact_id,
            "path": artifact.path,
            "mime": artifact.mime,
        }

    def _normalize_scrape_formats(self, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            return ["markdown"]
        formats = []
        for item in value:
            normalized = str(item or "").strip()
            if not normalized:
                continue
            if normalized not in self.SCRAPE_FORMATS:
                raise FirecrawlAgentError(
                    code="INVALID_INPUT",
                    message=f"Unsupported scrape format: {normalized}",
                    retryable=False,
                    next_action="revise_input",
                )
            formats.append(normalized)
        return self._dedupe_preserve_order(formats) or ["markdown"]

    def _require_url(self, value: Any, *, field_name: str) -> str:
        candidate = str(value or "").strip()
        parsed = urlparse(candidate)
        if not candidate or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise FirecrawlAgentError(
                code="INVALID_INPUT",
                message=f"{field_name} must be a valid http(s) URL.",
                retryable=False,
                next_action="revise_input",
            )
        return candidate

    def _normalize_url_list(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        normalized: list[str] = []
        for item in value:
            candidate = str(item or "").strip()
            if not candidate:
                continue
            parsed = urlparse(candidate)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise FirecrawlAgentError(
                    code="INVALID_INPUT",
                    message=f"Invalid URL in urls: {candidate}",
                    retryable=False,
                    next_action="revise_input",
                )
            normalized.append(candidate)
        return self._dedupe_preserve_order(normalized)[:10]

    def _normalize_string_list(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item for item in (str(entry or "").strip() for entry in value) if item]

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
            raise FirecrawlAgentError(
                code="INVALID_INPUT",
                message=f"Expected integer value, received {value!r}.",
                retryable=False,
                next_action="revise_input",
            ) from exc
        if minimum is not None and result < minimum:
            raise FirecrawlAgentError(
                code="INVALID_INPUT",
                message=f"Value must be >= {minimum}.",
                retryable=False,
                next_action="revise_input",
            )
        if maximum is not None and result > maximum:
            raise FirecrawlAgentError(
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

    def _clip_text(self, value: str, *, limit: int) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)].rstrip() + "..."

    def _json_loads_list(self, value: str) -> list[dict[str, Any]]:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    def _dedupe_preserve_order(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for item in values:
            if item in seen:
                continue
            seen.add(item)
            ordered.append(item)
        return ordered

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
