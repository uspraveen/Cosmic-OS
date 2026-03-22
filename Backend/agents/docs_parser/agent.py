from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from shared.agent_runtime import AgentRuntime
from shared.contracts import AgentError, AgentResult, ArtifactManifest, TaskEnvelope
from shared.sqlite_client import connect_sync
from shared import infer_document_mime_from_extension, is_supported_document_artifact

from .config import AGENT_ROOT, BACKEND_ROOT, DocsParserConfig
from .docling_adapter import DoclingAdapter, FullPageVlmRequest, ParseRequest, PictureDescriptionRequest
from .office_renderer import OfficeDocumentRenderer, RenderedOfficeDocument


logger = logging.getLogger(__name__)

_RUNS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS docs_parser_session_runs (
    task_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    bundle_id TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    artifact_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_docs_parser_session_runs_session_created
ON docs_parser_session_runs (session_id, created_at DESC);
"""


class DocsParserAgentError(RuntimeError):
    def __init__(self, *, code: str, message: str, retryable: bool, next_action: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.next_action = next_action


@dataclass(slots=True)
class AssetReinspectionRequest:
    api_key: str
    api_url: str
    model: str
    prompt: str
    timeout_sec: float
    max_new_tokens: int
    detail: str


class DocsParserAgent(AgentRuntime):
    PARSE_BUNDLE_INTENT = "docs.parse_bundle"
    BROWSE_BUNDLE_INTENT = "docs.browse_bundle"
    SEARCH_BUNDLE_INTENT = "docs.search_bundle"
    READ_BUNDLE_INTENT = "docs.read_bundle"
    FETCH_ASSET_INTENT = "docs.fetch_asset"
    REINSPECT_ASSET_INTENT = "docs.reinspect_asset"

    def __init__(
        self,
        *,
        redis_client,
        config: DocsParserConfig | None = None,
        instance_id: str | None = None,
        agent_secret: str | None = None,
        registry_db_path: str | Path | None = None,
        http_client=None,
        parser: DoclingAdapter | None = None,
        office_renderer: OfficeDocumentRenderer | None = None,
        agent_root: str | Path | None = None,
        artifacts_root: str | Path | None = None,
        store_root: str | Path | None = None,
        runtime_root: str | Path | None = None,
    ) -> None:
        self.config = config or DocsParserConfig.from_env()
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
        self.session_db_path = self.data_root / "docs_parser_session_runs.db"
        self.artifacts_root = (
            Path(artifacts_root).expanduser() if artifacts_root else BACKEND_ROOT / "runs" / "artifacts"
        ).resolve()
        self.parser = parser or DoclingAdapter()
        self.office_renderer = office_renderer or OfficeDocumentRenderer(
            binary_path=self.config.office_renderer_path,
            timeout_sec=self.config.office_render_timeout_sec,
        )

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
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.logs_root.mkdir(parents=True, exist_ok=True)
        self.artifacts_root.mkdir(parents=True, exist_ok=True)
        self.store_root.mkdir(parents=True, exist_ok=True)
        if not self.learnings_path.exists():
            self.learnings_path.write_text(
                "# Docs Parser Agent Learnings\n\n"
                "- Preserve a canonical parsed bundle per document.\n"
                "- Keep large parsed outputs in artifacts, not shared memory.\n"
                "- Expose stable document and chunk IDs for later retrieval.\n",
                encoding="utf-8",
            )
        self._initialize_store()

    async def execute(self, task: TaskEnvelope) -> AgentResult:
        try:
            if task.intent == self.PARSE_BUNDLE_INTENT:
                return await self._handle_parse_bundle(task)
            if task.intent == self.BROWSE_BUNDLE_INTENT:
                return await self._handle_browse_bundle(task)
            if task.intent == self.SEARCH_BUNDLE_INTENT:
                return await self._handle_search_bundle(task)
            if task.intent == self.READ_BUNDLE_INTENT:
                return await self._handle_read_bundle(task)
            if task.intent == self.FETCH_ASSET_INTENT:
                return await self._handle_fetch_asset(task)
            if task.intent == self.REINSPECT_ASSET_INTENT:
                return await self._handle_reinspect_asset(task)
            return self._result_error(
                code="INVALID_INPUT",
                message=f"Unsupported intent: {task.intent}",
                retryable=False,
                next_action="escalate",
            )
        except DocsParserAgentError as exc:
            logger.warning(
                "docs_parser_agent.handled_error task_id=%s intent=%s code=%s message=%s",
                task.task_id,
                task.intent,
                exc.code,
                exc.message,
            )
            return self._result_error(
                code=exc.code,
                message=exc.message,
                retryable=exc.retryable,
                next_action=exc.next_action,
            )
        except Exception as exc:
            logger.exception("docs_parser_agent.unhandled_error task_id=%s intent=%s", task.task_id, task.intent)
            return self._result_error(
                code="INTERNAL_ERROR",
                message=str(exc).strip()[:500] or "Docs parser agent failed unexpectedly.",
                retryable=False,
                next_action="escalate",
            )

    async def _handle_parse_bundle(self, task: TaskEnvelope) -> AgentResult:
        artifacts = self._normalize_input_artifacts(task.input_artifacts)
        if not artifacts:
            raise DocsParserAgentError(
                code="INVALID_INPUT",
                message="docs.parse_bundle requires one or more input document artifacts.",
                retryable=False,
                next_action="revise_input",
            )
        if len(artifacts) > self.config.max_input_artifacts:
            raise DocsParserAgentError(
                code="INVALID_INPUT",
                message=f"docs.parse_bundle supports at most {self.config.max_input_artifacts} input artifacts per task.",
                retryable=False,
                next_action="revise_input",
            )

        parse_request = self._build_parse_request(task.input)
        bundle_id = f"bundle_{uuid4().hex[:12]}"
        bundle_label = self._safe_text(task.input.get("bundle_label")) or None
        document_summaries: list[dict[str, Any]] = []
        produced_artifacts: list[ArtifactManifest] = []

        parallelism = max(1, min(len(artifacts), self.config.max_parallel_documents))
        await self._emit_progress(
            task.task_id,
            f"Parsing {len(artifacts)} uploaded document(s) with up to {parallelism} documents in parallel.",
        )
        semaphore = asyncio.Semaphore(parallelism)
        results = await asyncio.gather(
            *[
                self._parse_bundle_artifact(
                    task=task,
                    artifact=artifact,
                    index=index,
                    total=len(artifacts),
                    bundle_id=bundle_id,
                    parse_request=parse_request,
                    semaphore=semaphore,
                )
                for index, artifact in enumerate(artifacts, start=1)
            ],
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                if isinstance(result, DocsParserAgentError):
                    raise result
                raise result
            _, summary, manifests = result
            document_summaries.append(summary)
            produced_artifacts.extend(manifests)

        output = {
            "response": f"Parsed {len(document_summaries)} document(s) into canonical bundles.",
            "bundle_id": bundle_id,
            "bundle_label": bundle_label,
            "document_count": len(document_summaries),
            "documents": document_summaries,
        }
        self._record_session_run(
            task=task,
            bundle_id=bundle_id,
            summary=output,
            artifacts=produced_artifacts,
        )
        return AgentResult(status="completed", output=output, artifacts=produced_artifacts, error=None)

    async def _parse_bundle_artifact(
        self,
        *,
        task: TaskEnvelope,
        artifact: dict[str, str],
        index: int,
        total: int,
        bundle_id: str,
        parse_request: ParseRequest,
        semaphore: asyncio.Semaphore,
    ) -> tuple[int, dict[str, Any], list[ArtifactManifest]]:
        async with semaphore:
            source_path = self._resolve_input_artifact_path(artifact)
            source_path = self._verify_source_file(source_path=source_path, artifact=artifact)
            await self._emit_progress(
                task.task_id,
                f"Parsing document {index}/{total}: {artifact.get('filename') or artifact['artifact_id']}.",
            )
            try:
                parsed, enrichment_status = await asyncio.to_thread(
                    self._parse_with_enrichment_fallback,
                    source_path=source_path,
                    artifact=artifact,
                    request=parse_request,
                )
                parsed, enrichment_status, rendered_office_document = await self._maybe_apply_full_page_vlm_escalation(
                    task=task,
                    source_path=source_path,
                    artifact=artifact,
                    request=parse_request,
                    parsed=parsed,
                    enrichment_status=enrichment_status,
                )
            except Exception as exc:
                text = str(exc).strip() or f"Failed to parse {artifact.get('filename') or artifact['artifact_id']}."
                code, retryable, next_action = self._classify_parse_failure(text)
                raise DocsParserAgentError(
                    code=code,
                    message=text,
                    retryable=retryable,
                    next_action=next_action,
                ) from exc
            doc_id = self._stable_id(f"{artifact['artifact_id']}:{source_path.name}:{parsed.title or ''}")
            summary, manifests = await asyncio.to_thread(
                self._persist_parsed_bundle,
                task=task,
                artifact=artifact,
                doc_id=doc_id,
                bundle_id=bundle_id,
                source_path=source_path,
                parsed=parsed,
                parse_request=parse_request,
                enrichment_status=enrichment_status,
                rendered_office_document=rendered_office_document,
            )
            return index, summary, manifests

    async def _handle_browse_bundle(self, task: TaskEnvelope) -> AgentResult:
        bundle_id = self._require_bundle_id(task.input.get("bundle_id"))
        index_kind = self._safe_text(task.input.get("index_kind")).lower() or "documents"
        if index_kind not in {"documents", "sections", "pages", "slides", "chunks", "tables", "figures", "assets"}:
            raise DocsParserAgentError(
                code="INVALID_INPUT",
                message="index_kind must be one of: documents, sections, pages, slides, chunks, tables, figures, assets.",
                retryable=False,
                next_action="revise_input",
            )

        bundle_output = self._load_bundle_output(bundle_id)
        documents = bundle_output.get("documents") if isinstance(bundle_output.get("documents"), list) else []
        if index_kind == "documents":
            normalized_documents = [item for item in documents if isinstance(item, dict)]
            requested_doc_id = self._safe_text(task.input.get("doc_id"))
            if requested_doc_id:
                selected_document = self._select_document_for_read(normalized_documents, doc_id=requested_doc_id)
                chunk_index = self._load_json_artifact(selected_document, "chunk_index")
                return AgentResult(
                    status="completed",
                    output={
                        "response": f"Loaded document index for parsed bundle {bundle_id}.",
                        "bundle_id": bundle_id,
                        "index_kind": "documents",
                        "document_count": len(normalized_documents),
                        "doc_id": selected_document["doc_id"],
                        "title": selected_document.get("title"),
                        "document_index": self._document_index_payload(selected_document, chunk_index),
                    },
                    artifacts=[],
                    error=None,
                )
            return AgentResult(
                status="completed",
                output={
                    "response": f"Loaded {len(normalized_documents)} document summary item(s) from parsed bundle {bundle_id}.",
                    "bundle_id": bundle_id,
                    "index_kind": "documents",
                    "document_count": len(normalized_documents),
                    "documents": normalized_documents,
                },
                artifacts=[],
                error=None,
            )

        selected_document = self._select_document_for_read(
            documents,
            doc_id=self._safe_text(task.input.get("doc_id")),
        )
        chunk_index = self._load_json_artifact(selected_document, "chunk_index")
        limit = self._coerce_positive_int(task.input.get("limit"), default=20, minimum=1, maximum=100)
        browse_payload = self._browse_document_index(
            index_kind=index_kind,
            selected_document=selected_document,
            chunk_index=chunk_index,
            limit=limit,
        )
        return AgentResult(
            status="completed",
            output=browse_payload | {"bundle_id": bundle_id},
            artifacts=[],
            error=None,
        )

    async def _handle_search_bundle(self, task: TaskEnvelope) -> AgentResult:
        bundle_id = self._require_bundle_id(task.input.get("bundle_id"))
        query = self._safe_text(task.input.get("query"))
        if not query:
            raise DocsParserAgentError(
                code="INVALID_INPUT",
                message="docs.search_bundle requires a non-empty query.",
                retryable=False,
                next_action="revise_input",
            )
        search_kind = self._safe_text(task.input.get("search_kind")).lower() or "chunks"
        if search_kind not in {"chunks", "sections"}:
            raise DocsParserAgentError(
                code="INVALID_INPUT",
                message="search_kind must be one of: chunks, sections.",
                retryable=False,
                next_action="revise_input",
            )
        limit = self._coerce_positive_int(task.input.get("limit"), default=5, minimum=1, maximum=12)
        bundle_output = self._load_bundle_output(bundle_id)
        doc_ids = [
            self._safe_text(item)
            for item in (task.input.get("doc_ids") if isinstance(task.input.get("doc_ids"), list) else [])
            if self._safe_text(item)
        ]
        matches = self._search_bundle(bundle_output, query=query, limit=limit, search_kind=search_kind, doc_ids=doc_ids)
        return AgentResult(
            status="completed",
            output={
                "response": f"Found {len(matches)} matching {search_kind.rstrip('s')}(s) in bundle {bundle_id}.",
                "bundle_id": bundle_id,
                "query": query,
                "search_kind": search_kind,
                "count": len(matches),
                "matches": matches,
            },
            artifacts=[],
            error=None,
        )

    async def _handle_read_bundle(self, task: TaskEnvelope) -> AgentResult:
        bundle_id = self._require_bundle_id(task.input.get("bundle_id"))
        bundle_output = self._load_bundle_output(bundle_id)
        documents = bundle_output.get("documents") if isinstance(bundle_output.get("documents"), list) else []
        requested_doc_id = self._safe_text(task.input.get("doc_id"))
        selected_document = self._select_document_for_read(documents, doc_id=requested_doc_id)
        max_chars = self._coerce_positive_int(task.input.get("max_chars"), default=5000, minimum=500, maximum=12000)
        read_kind = self._safe_text(task.input.get("read_kind")).lower()
        section_id = self._safe_text(task.input.get("section_id"))
        requested_chunk_ids = [
            self._safe_text(item)
            for item in (task.input.get("chunk_ids") if isinstance(task.input.get("chunk_ids"), list) else [])
            if self._safe_text(item)
        ]
        if not read_kind:
            if section_id:
                read_kind = "section"
            elif requested_chunk_ids:
                read_kind = "chunk_ids"
            else:
                read_kind = "document"
        start_page = self._coerce_positive_int(task.input.get("start_page"), default=1, minimum=1, maximum=100000)
        end_page = self._coerce_positive_int(task.input.get("end_page"), default=start_page, minimum=1, maximum=100000)
        start_slide = self._coerce_positive_int(task.input.get("start_slide"), default=1, minimum=1, maximum=100000)
        end_slide = self._coerce_positive_int(task.input.get("end_slide"), default=start_slide, minimum=1, maximum=100000)
        offset_chars = self._coerce_positive_int(task.input.get("offset_chars"), default=0, minimum=0, maximum=2_000_000)
        before_chars = self._coerce_positive_int(task.input.get("before_chars"), default=1500, minimum=0, maximum=12000)
        after_chars = self._coerce_positive_int(task.input.get("after_chars"), default=3500, minimum=0, maximum=12000)
        anchor_id = self._safe_text(task.input.get("anchor_id"))
        content, mode, citations, extra = self._read_bundle_content(
            selected_document,
            read_kind=read_kind,
            section_id=section_id,
            chunk_ids=requested_chunk_ids,
            start_page=start_page,
            end_page=end_page,
            start_slide=start_slide,
            end_slide=end_slide,
            anchor_id=anchor_id,
            offset_chars=offset_chars,
            before_chars=before_chars,
            after_chars=after_chars,
            max_chars=max_chars,
        )
        return AgentResult(
            status="completed",
            output={
                "response": f"Loaded {mode} from parsed bundle {bundle_id}.",
                "bundle_id": bundle_id,
                "doc_id": selected_document["doc_id"],
                "title": selected_document.get("title"),
                "mode": mode,
                "content": content,
                "citations": citations,
            } | extra,
            artifacts=[],
            error=None,
        )

    async def _handle_fetch_asset(self, task: TaskEnvelope) -> AgentResult:
        bundle_id = self._require_bundle_id(task.input.get("bundle_id"))
        asset_id = self._safe_text(task.input.get("asset_id"))
        if not asset_id:
            raise DocsParserAgentError(
                code="INVALID_INPUT",
                message="asset_id is required.",
                retryable=False,
                next_action="revise_input",
            )
        max_chars = self._coerce_positive_int(task.input.get("max_chars"), default=5000, minimum=500, maximum=12000)
        bundle_output = self._load_bundle_output(bundle_id)
        requested_doc_id = self._safe_text(task.input.get("doc_id"))
        resolved = self._resolve_bundle_asset(
            bundle_output=bundle_output,
            asset_id=asset_id,
            requested_doc_id=requested_doc_id,
        )
        path = self._safe_text(resolved["asset"].get("path"))
        logical_path = path or None
        content = None
        mime = self._safe_text(resolved["asset"].get("mime")) or None
        if path:
            file_path = self._resolve_document_path(path)
            if file_path.exists() and file_path.is_file() and mime and mime.startswith("text/"):
                content = file_path.read_text(encoding="utf-8")[:max_chars]
        cached_reinspection = self._load_cached_reinspection(
            document=resolved["document"],
            asset_id=asset_id,
            question="",
        )
        return AgentResult(
            status="completed",
            output={
                "response": f"Loaded asset {asset_id} from parsed bundle {bundle_id}.",
                "bundle_id": bundle_id,
                "doc_id": self._safe_text(resolved["document"].get("doc_id")),
                "asset_id": asset_id,
                "asset": resolved["asset"],
                "figure": resolved["figure"],
                "table": resolved["table"],
                "content": content,
                "path": logical_path,
                "reinspection": cached_reinspection.get("analysis") if cached_reinspection else None,
                "reinspection_path": cached_reinspection.get("path") if cached_reinspection else None,
            },
            artifacts=[],
            error=None,
        )

    async def _handle_reinspect_asset(self, task: TaskEnvelope) -> AgentResult:
        bundle_id = self._require_bundle_id(task.input.get("bundle_id"))
        asset_id = self._safe_text(task.input.get("asset_id"))
        if not asset_id:
            raise DocsParserAgentError(
                code="INVALID_INPUT",
                message="asset_id is required.",
                retryable=False,
                next_action="revise_input",
            )
        bundle_output = self._load_bundle_output(bundle_id)
        resolved = self._resolve_bundle_asset(
            bundle_output=bundle_output,
            asset_id=asset_id,
            requested_doc_id=self._safe_text(task.input.get("doc_id")),
        )
        asset = resolved["asset"]
        mime = self._safe_text(asset.get("mime")).lower()
        path = self._safe_text(asset.get("path"))
        if not mime.startswith("image/"):
            raise DocsParserAgentError(
                code="INVALID_INPUT",
                message=f"Asset {asset_id} is not an image asset and cannot be visually reinspected.",
                retryable=False,
                next_action="revise_input",
            )
        if not path:
            raise DocsParserAgentError(
                code="MISSING_ARTIFACT",
                message=f"Image asset path is missing for {asset_id}.",
                retryable=False,
                next_action="escalate",
            )
        image_path = self._resolve_document_path(path)
        if not image_path.exists() or not image_path.is_file():
            raise DocsParserAgentError(
                code="MISSING_ARTIFACT",
                message=f"Image asset file is missing for {asset_id}.",
                retryable=False,
                next_action="escalate",
            )
        question = self._safe_text(task.input.get("question"))
        cached = self._load_cached_reinspection(document=resolved["document"], asset_id=asset_id, question=question)
        if cached is not None:
            return AgentResult(
                status="completed",
                output={
                    "response": f"Loaded cached visual reinspection for {asset_id}.",
                    "bundle_id": bundle_id,
                    "doc_id": self._safe_text(resolved["document"].get("doc_id")),
                    "asset_id": asset_id,
                    "asset": asset,
                    "figure": resolved["figure"],
                    "table": resolved["table"],
                    "cached": True,
                    "question": question or None,
                    "analysis": cached.get("analysis"),
                    "reinspection_path": cached.get("path"),
                },
                artifacts=[],
                error=None,
            )

        request = self._build_asset_reinspection_request()
        if request is None:
            raise DocsParserAgentError(
                code="PARSER_UNAVAILABLE",
                message="Asset reinspection requires hosted VLM configuration.",
                retryable=False,
                next_action="escalate",
            )
        await self._emit_progress(
            task.task_id,
            f"Reinspecting visual asset {asset_id} for exact slide or figure understanding.",
        )
        analysis = await self._run_asset_reinspection(
            request=request,
            image_path=image_path,
            document=resolved["document"],
            asset=asset,
            figure=resolved["figure"],
            table=resolved["table"],
            question=question,
        )
        cache_path = self._write_reinspection_cache(
            document=resolved["document"],
            asset_id=asset_id,
            question=question,
            payload={
                "bundle_id": bundle_id,
                "doc_id": self._safe_text(resolved["document"].get("doc_id")),
                "asset_id": asset_id,
                "question": question or None,
                "analysis": analysis,
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "model": request.model,
            },
        )
        return AgentResult(
            status="completed",
            output={
                "response": f"Completed visual reinspection for asset {asset_id}.",
                "bundle_id": bundle_id,
                "doc_id": self._safe_text(resolved["document"].get("doc_id")),
                "asset_id": asset_id,
                "asset": asset,
                "figure": resolved["figure"],
                "table": resolved["table"],
                "cached": False,
                "question": question or None,
                "analysis": analysis,
                "reinspection_path": self._logical_artifact_path(cache_path),
            },
            artifacts=[],
            error=None,
        )

    def _resolve_bundle_asset(
        self,
        *,
        bundle_output: dict[str, Any],
        asset_id: str,
        requested_doc_id: str,
    ) -> dict[str, Any]:
        documents = bundle_output.get("documents") if isinstance(bundle_output.get("documents"), list) else []
        selected_documents = [self._select_document_for_read(documents, doc_id=requested_doc_id)] if requested_doc_id else [
            item for item in documents if isinstance(item, dict)
        ]
        for document in selected_documents:
            chunk_index = self._load_json_artifact(document, "chunk_index")
            figures = chunk_index.get("figures") if isinstance(chunk_index.get("figures"), list) else []
            tables = chunk_index.get("tables") if isinstance(chunk_index.get("tables"), list) else []
            for asset in chunk_index.get("assets", []) if isinstance(chunk_index.get("assets"), list) else []:
                if not isinstance(asset, dict):
                    continue
                if self._safe_text(asset.get("asset_id")) != asset_id:
                    continue
                return {
                    "document": document,
                    "asset": asset,
                    "figure": self._match_related_entry(figures, "asset_id", asset_id),
                    "table": self._match_related_entry(tables, "asset_id", asset_id),
                }
        raise DocsParserAgentError(
            code="INVALID_INPUT",
            message=f"asset_id was not found in the parsed bundle: {asset_id}",
            retryable=False,
            next_action="revise_input",
        )

    def _match_related_entry(self, entries: list[Any], key: str, expected: str) -> dict[str, Any] | None:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if self._safe_text(entry.get(key)) == expected:
                return entry
        return None

    def _build_asset_reinspection_request(self) -> AssetReinspectionRequest | None:
        if not self.config.default_enable_asset_reinspection:
            return None
        api_key = self._safe_text(self.config.asset_reinspection_api_key)
        api_url = self._safe_text(self.config.asset_reinspection_api_url)
        model = self._safe_text(self.config.asset_reinspection_model)
        prompt = self._safe_text(self.config.asset_reinspection_prompt)
        detail = self._safe_text(self.config.asset_reinspection_detail).lower() or "high"
        if detail not in {"low", "high", "auto"}:
            detail = "high"
        if not api_key or not api_url or not model or not prompt:
            return None
        return AssetReinspectionRequest(
            api_key=api_key,
            api_url=api_url,
            model=model,
            prompt=prompt,
            timeout_sec=self.config.asset_reinspection_timeout_sec,
            max_new_tokens=self.config.asset_reinspection_max_new_tokens,
            detail=detail,
        )

    async def _run_asset_reinspection(
        self,
        *,
        request: AssetReinspectionRequest,
        image_path: Path,
        document: dict[str, Any],
        asset: dict[str, Any],
        figure: dict[str, Any] | None,
        table: dict[str, Any] | None,
        question: str,
    ) -> dict[str, Any]:
        image_bytes = image_path.read_bytes()
        data_url = self._image_data_url(image_bytes=image_bytes, mime=self._safe_text(asset.get("mime")) or "image/png")
        context_parts = [
            f"Document title: {self._safe_text(document.get('title')) or 'unknown'}",
            f"Filename: {self._safe_text(document.get('filename')) or 'unknown'}",
            f"Asset kind: {self._safe_text(asset.get('kind')) or 'unknown'}",
            f"Asset id: {self._safe_text(asset.get('asset_id')) or 'unknown'}",
        ]
        if figure:
            classification = figure.get("classification") if isinstance(figure.get("classification"), dict) else {}
            context_parts.extend(
                [
                    f"Figure caption: {self._safe_text(figure.get('caption')) or 'none'}",
                    f"Figure page number: {self._safe_text(figure.get('page_number')) or 'unknown'}",
                    f"Figure slide number: {self._safe_text(figure.get('slide_number')) or 'unknown'}",
                    f"Existing parsed description: {self._safe_text(figure.get('description')) or 'none'}",
                    f"Existing parsed classification: {self._safe_text(classification.get('label')) or 'none'}",
                ]
            )
        if table:
            context_parts.extend(
                [
                    f"Table title: {self._safe_text(table.get('title')) or 'none'}",
                    f"Table page number: {self._safe_text(table.get('page_number')) or 'unknown'}",
                    f"Table slide number: {self._safe_text(table.get('slide_number')) or 'unknown'}",
                ]
            )
        if question:
            context_parts.append(f"Specific follow-up question: {question}")
        payload = {
            "model": request.model,
            "temperature": 0,
            "max_tokens": request.max_new_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": request.prompt},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "\n".join(context_parts),
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": data_url,
                                "detail": request.detail,
                            },
                        },
                    ],
                },
            ],
        }
        response = await self._http_client.post(
            request.api_url,
            headers={
                "Authorization": f"Bearer {request.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=request.timeout_sec,
        )
        if response.status_code >= 400:
            raise DocsParserAgentError(
                code="PARSER_UNAVAILABLE",
                message=f"Asset reinspection API error (status={response.status_code}): {response.text[:240]}",
                retryable=response.status_code == 429 or response.status_code >= 500,
                next_action="escalate",
            )
        try:
            response_payload = response.json()
        except json.JSONDecodeError as exc:
            raise DocsParserAgentError(
                code="INTERNAL_ERROR",
                message="Asset reinspection response was not valid JSON.",
                retryable=False,
                next_action="escalate",
            ) from exc
        content = self._extract_chat_message_content(response_payload)
        analysis = self._parse_json_object(content)
        return self._normalize_asset_reinspection_output(analysis)

    def _extract_chat_message_content(self, payload: dict[str, Any]) -> str:
        choices = payload.get("choices") if isinstance(payload.get("choices"), list) else []
        if not choices:
            raise DocsParserAgentError(
                code="INTERNAL_ERROR",
                message="Asset reinspection response did not contain any choices.",
                retryable=False,
                next_action="escalate",
            )
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise DocsParserAgentError(
                code="INTERNAL_ERROR",
                message="Asset reinspection response did not contain a message payload.",
                retryable=False,
                next_action="escalate",
            )
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = self._safe_text(item.get("text"))
                if text:
                    text_parts.append(text)
            if text_parts:
                return "\n".join(text_parts)
        raise DocsParserAgentError(
            code="INTERNAL_ERROR",
            message="Asset reinspection response did not contain textual content.",
            retryable=False,
            next_action="escalate",
        )

    def _parse_json_object(self, raw: str) -> dict[str, Any]:
        text = raw.strip()
        if not text:
            raise DocsParserAgentError(
                code="INTERNAL_ERROR",
                message="Asset reinspection returned an empty response.",
                retryable=False,
                next_action="escalate",
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end <= start:
                raise DocsParserAgentError(
                    code="INTERNAL_ERROR",
                    message="Asset reinspection did not return a JSON object.",
                    retryable=False,
                    next_action="escalate",
                ) from None
            payload = json.loads(text[start : end + 1])
        if not isinstance(payload, dict):
            raise DocsParserAgentError(
                code="INTERNAL_ERROR",
                message="Asset reinspection returned a non-object JSON payload.",
                retryable=False,
                next_action="escalate",
            )
        return payload

    def _normalize_asset_reinspection_output(self, payload: dict[str, Any]) -> dict[str, Any]:
        def _string_list(value: Any, *, limit: int = 16) -> list[str]:
            if not isinstance(value, list):
                return []
            normalized = [self._safe_text(item) for item in value]
            return [item for item in normalized if item][:limit]

        confidence = self._safe_text(payload.get("confidence")).lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "medium"
        visual_type = self._safe_text(payload.get("visual_type")).lower() or "other"
        return {
            "summary": self._safe_text(payload.get("summary"))[:1600] or "No visual summary returned.",
            "visual_type": visual_type,
            "visible_text": _string_list(payload.get("visible_text")),
            "chart_observations": _string_list(payload.get("chart_observations")),
            "diagram_relationships": _string_list(payload.get("diagram_relationships")),
            "design_observations": _string_list(payload.get("design_observations")),
            "key_entities": _string_list(payload.get("key_entities")),
            "uncertainties": _string_list(payload.get("uncertainties")),
            "confidence": confidence,
        }

    def _image_data_url(self, *, image_bytes: bytes, mime: str) -> str:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    def _reinspection_cache_path(self, *, document: dict[str, Any], asset_id: str, question: str) -> Path:
        bundle_root = self._document_path(document, "manifest").parent
        cache_root = bundle_root / "analysis" / "reinspection"
        suffix = "base" if not question else self._stable_id(question.lower())
        return cache_root / f"{asset_id}_{suffix}.json"

    def _load_cached_reinspection(self, *, document: dict[str, Any], asset_id: str, question: str) -> dict[str, Any] | None:
        cache_path = self._reinspection_cache_path(document=document, asset_id=asset_id, question=question)
        if not cache_path.exists() or not cache_path.is_file():
            return None
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        payload["path"] = self._logical_artifact_path(cache_path)
        return payload

    def _write_reinspection_cache(
        self,
        *,
        document: dict[str, Any],
        asset_id: str,
        question: str,
        payload: dict[str, Any],
    ) -> Path:
        cache_path = self._reinspection_cache_path(document=document, asset_id=asset_id, question=question)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return cache_path

    def _build_parse_request(self, payload: dict[str, Any]) -> ParseRequest:
        ocr_mode = self._safe_text(payload.get("ocr_mode")) or "auto"
        if ocr_mode not in {"auto", "off", "force"}:
            raise DocsParserAgentError(
                code="INVALID_INPUT",
                message="ocr_mode must be one of: auto, off, force.",
                retryable=False,
                next_action="revise_input",
            )
        full_page_vlm_mode = self._safe_text(payload.get("full_page_vlm_mode")) or "auto"
        if full_page_vlm_mode not in {"auto", "off", "force"}:
            raise DocsParserAgentError(
                code="INVALID_INPUT",
                message="full_page_vlm_mode must be one of: auto, off, force.",
                retryable=False,
                next_action="revise_input",
            )
        enable_ocr = self.config.default_enable_ocr if ocr_mode == "auto" else (ocr_mode == "force")
        picture_description = self._build_picture_description_request()
        full_page_vlm = self._build_full_page_vlm_request(enabled=full_page_vlm_mode != "off")
        if full_page_vlm_mode == "force" and full_page_vlm is None:
            raise DocsParserAgentError(
                code="INVALID_INPUT",
                message="full_page_vlm_mode=force requires hosted full-page VLM configuration.",
                retryable=False,
                next_action="revise_input",
            )
        generate_picture_images = self._coerce_bool(
            payload.get("generate_picture_images"),
            default=self.config.default_generate_picture_images,
        )
        if picture_description is not None:
            generate_picture_images = True
        return ParseRequest(
            enable_ocr=enable_ocr,
            generate_page_images=self._coerce_bool(
                payload.get("generate_page_images"),
                default=self.config.default_generate_page_images,
            ),
            generate_picture_images=generate_picture_images,
            picture_description=picture_description,
            full_page_vlm_mode=full_page_vlm_mode,
            use_full_page_vlm=False,
            full_page_vlm=full_page_vlm,
            max_file_size_bytes=self.config.max_input_file_bytes,
            max_num_pages=self.config.max_num_pages,
            max_chunk_chars=self.config.max_chunk_chars,
            chunk_overlap_chars=self.config.chunk_overlap_chars,
        )

    def _build_picture_description_request(self) -> PictureDescriptionRequest | None:
        if not self.config.default_enable_picture_description:
            return None
        api_key = self._safe_text(self.config.picture_description_api_key)
        api_url = self._safe_text(self.config.picture_description_api_url)
        model = self._safe_text(self.config.picture_description_model)
        preset = self._safe_text(self.config.picture_description_preset) or "qwen"
        prompt = self._safe_text(self.config.picture_description_prompt)
        if not api_key or not api_url or not model:
            return None
        return PictureDescriptionRequest(
            api_key=api_key,
            api_url=api_url,
            model=model,
            preset=preset,
            prompt=prompt,
            timeout_sec=self.config.picture_description_timeout_sec,
            concurrency=self.config.picture_description_concurrency,
            batch_size=self.config.picture_description_batch_size,
            max_new_tokens=self.config.picture_description_max_new_tokens,
            scale=self.config.picture_description_scale,
            picture_area_threshold=self.config.picture_description_area_threshold,
            classification_min_confidence=self.config.picture_description_classification_min_confidence,
            classification_deny=tuple(self.config.picture_description_classification_deny),
        )

    def _build_full_page_vlm_request(self, *, enabled: bool) -> FullPageVlmRequest | None:
        if not enabled or not self.config.default_enable_full_page_vlm:
            return None
        api_key = self._safe_text(self.config.full_page_vlm_api_key)
        api_url = self._safe_text(self.config.full_page_vlm_api_url)
        model = self._safe_text(self.config.full_page_vlm_model)
        preset = self._safe_text(self.config.full_page_vlm_preset) or "qwen"
        if not api_key or not api_url or not model:
            return None
        return FullPageVlmRequest(
            api_key=api_key,
            api_url=api_url,
            model=model,
            preset=preset,
            timeout_sec=self.config.full_page_vlm_timeout_sec,
            concurrency=self.config.full_page_vlm_concurrency,
            batch_size=self.config.full_page_vlm_batch_size,
            max_new_tokens=self.config.full_page_vlm_max_new_tokens,
            scale=self.config.full_page_vlm_scale,
        )

    def _parse_with_enrichment_fallback(
        self,
        *,
        source_path: Path,
        artifact: dict[str, str],
        request: ParseRequest,
    ) -> tuple[Any, dict[str, Any]]:
        try:
            parsed = self.parser.parse_file(
                file_path=source_path,
                artifact_id=artifact["artifact_id"],
                mime_type=artifact["mime"],
                request=request,
                source_filename=artifact["filename"],
            )
            return parsed, self._enrichment_status(request, applied=True, fallback_reason=None)
        except Exception as exc:
            if request.picture_description is None or not self._should_retry_without_picture_description(exc):
                raise
            fallback_request = replace(request, picture_description=None)
            parsed = self.parser.parse_file(
                file_path=source_path,
                artifact_id=artifact["artifact_id"],
                mime_type=artifact["mime"],
                request=fallback_request,
                source_filename=artifact["filename"],
            )
            return parsed, self._enrichment_status(
                request,
                applied=False,
                fallback_reason=str(exc).strip() or type(exc).__name__,
            )

    def _should_retry_without_picture_description(self, exc: Exception) -> bool:
        normalized = (str(exc) or type(exc).__name__).strip().lower()
        return any(
            marker in normalized
            for marker in (
                "picture description",
                "remote services",
                "api_openai",
                "openai",
                "authentication",
                "unauthorized",
                "forbidden",
                "rate limit",
                "timed out",
                "timeout",
                "connection",
                "ssl",
            )
        )

    def _enrichment_status(
        self,
        request: ParseRequest,
        *,
        applied: bool,
        fallback_reason: str | None,
    ) -> dict[str, Any]:
        if request.picture_description is None:
            return {
                "picture_description_requested": False,
                "picture_description_applied": False,
                "picture_description_model": None,
                "picture_description_fallback_reason": None,
            }
        return {
            "picture_description_requested": True,
            "picture_description_applied": applied,
            "picture_description_model": request.picture_description.model,
            "picture_description_fallback_reason": fallback_reason,
        }

    async def _maybe_apply_full_page_vlm_escalation(
        self,
        *,
        task: TaskEnvelope,
        source_path: Path,
        artifact: dict[str, str],
        request: ParseRequest,
        parsed,
        enrichment_status: dict[str, Any],
    ) -> tuple[Any, dict[str, Any], RenderedOfficeDocument | None]:
        status = self._with_full_page_vlm_defaults(enrichment_status)
        if not self._is_office_document(artifact):
            return parsed, status, None

        analysis = self._analyze_image_heavy_document(parsed)
        status["image_heavy_analysis"] = analysis
        forced_by_policy = request.full_page_vlm_mode == "force"
        should_escalate = forced_by_policy
        if request.full_page_vlm_mode == "auto" and self._is_presentation_document(artifact):
            should_escalate = True
            analysis["should_escalate"] = True
            analysis["reasons"] = ["pptx_visual_first_default", *analysis.get("reasons", [])]
        if forced_by_policy:
            analysis["should_escalate"] = True
            analysis["reasons"] = ["full_page_vlm_mode=force", *analysis.get("reasons", [])]
        if analysis.get("should_escalate"):
            should_escalate = True
        if not should_escalate:
            return parsed, status, None
        if request.full_page_vlm is None:
            status["escalation_reason"] = "; ".join(analysis.get("reasons") or ["full-page VLM unavailable"])
            status["full_page_vlm_fallback_reason"] = "Hosted full-page VLM is not configured."
            return parsed, status, None
        if not self.config.enable_office_render_fallback:
            status["escalation_reason"] = "; ".join(analysis.get("reasons") or ["full-page VLM disabled"])
            status["office_render_fallback_reason"] = "Office rendering fallback is disabled."
            status["full_page_vlm_fallback_reason"] = "Office rendering fallback is disabled."
            return parsed, status, None

        status["escalation_reason"] = "; ".join(analysis.get("reasons") or ["image-heavy office document"])
        status["office_render_requested"] = True
        status["full_page_vlm_requested"] = True
        status["full_page_vlm_model"] = request.full_page_vlm.model

        rendered_office_document: RenderedOfficeDocument | None = None
        try:
            await self._emit_progress(
                task.task_id,
                f"Escalating {artifact.get('filename') or artifact['artifact_id']} through Office render and hosted full-page VLM.",
            )
            rendered_office_document = await asyncio.to_thread(
                self.office_renderer.render_to_pdf,
                source_path=source_path,
                working_root=self.runtime_root / "office_render",
            )
            status["office_render_applied"] = True
            status["office_render_backend"] = rendered_office_document.backend
            rerun_request = replace(
                request,
                enable_ocr=True,
                generate_page_images=True,
                generate_picture_images=True,
                picture_description=None,
                use_full_page_vlm=True,
            )
            rerun_parsed = await asyncio.to_thread(
                self.parser.parse_file,
                file_path=rendered_office_document.rendered_pdf_path,
                artifact_id=artifact["artifact_id"],
                mime_type=artifact["mime"],
                request=rerun_request,
                source_filename=artifact["filename"],
            )
            status["full_page_vlm_applied"] = True
            return rerun_parsed, status, rendered_office_document
        except Exception as exc:
            reason = str(exc).strip() or type(exc).__name__
            if rendered_office_document is None:
                status["office_render_fallback_reason"] = reason
            status["full_page_vlm_fallback_reason"] = reason
            if rendered_office_document is None:
                return parsed, status, None
            try:
                fallback_request = replace(
                    request,
                    enable_ocr=True,
                    generate_page_images=True,
                    generate_picture_images=True,
                    use_full_page_vlm=False,
                )
                fallback_parsed, fallback_enrichment = await asyncio.to_thread(
                    self._parse_with_enrichment_fallback,
                    source_path=rendered_office_document.rendered_pdf_path,
                    artifact=artifact,
                    request=fallback_request,
                )
                for key in (
                    "picture_description_requested",
                    "picture_description_applied",
                    "picture_description_model",
                    "picture_description_fallback_reason",
                ):
                    if key in fallback_enrichment:
                        status[key] = fallback_enrichment[key]
                status["full_page_vlm_requested"] = True
                status["full_page_vlm_model"] = request.full_page_vlm.model
                status["office_render_requested"] = True
                status["office_render_applied"] = True
                status["office_render_backend"] = rendered_office_document.backend
                status["escalation_reason"] = "; ".join(analysis.get("reasons") or ["image-heavy office document"])
                status["full_page_vlm_fallback_reason"] = reason
                return fallback_parsed, status, rendered_office_document
            except Exception:
                return parsed, status, rendered_office_document

    def _with_full_page_vlm_defaults(self, status: dict[str, Any]) -> dict[str, Any]:
        merged = dict(status)
        merged.setdefault("full_page_vlm_requested", False)
        merged.setdefault("full_page_vlm_applied", False)
        merged.setdefault("full_page_vlm_model", None)
        merged.setdefault("full_page_vlm_fallback_reason", None)
        merged.setdefault("office_render_requested", False)
        merged.setdefault("office_render_applied", False)
        merged.setdefault("office_render_backend", None)
        merged.setdefault("office_render_fallback_reason", None)
        merged.setdefault("image_heavy_analysis", None)
        merged.setdefault("escalation_reason", None)
        return merged

    def _is_office_document(self, artifact: dict[str, str]) -> bool:
        mime = self._safe_text(artifact.get("mime")).lower()
        filename = self._safe_text(artifact.get("filename")) or self._safe_text(artifact.get("path"))
        ext = Path(filename).suffix.lower()
        return ext in {".docx", ".pptx"} or mime in {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        }

    def _is_presentation_document(self, artifact: dict[str, str]) -> bool:
        mime = self._safe_text(artifact.get("mime")).lower()
        filename = self._safe_text(artifact.get("filename")) or self._safe_text(artifact.get("path"))
        ext = Path(filename).suffix.lower()
        return ext == ".pptx" or mime == "application/vnd.openxmlformats-officedocument.presentationml.presentation"

    def _analyze_image_heavy_document(self, parsed) -> dict[str, Any]:
        chunk_index = parsed.chunk_index if isinstance(getattr(parsed, "chunk_index", None), dict) else {}
        slides = chunk_index.get("slides") if isinstance(chunk_index.get("slides"), list) else []
        pages = chunk_index.get("pages") if isinstance(chunk_index.get("pages"), list) else []
        units = slides or pages
        unit_kind = "slides" if slides else "pages"
        if not units:
            return {
                "unit_kind": unit_kind,
                "unit_count": 0,
                "avg_text_chars_per_unit": 0.0,
                "low_text_unit_count": 0,
                "low_text_unit_ratio": 0.0,
                "visual_unit_count": 0,
                "visual_unit_ratio": 0.0,
                "chunk_density": 0.0,
                "should_escalate": False,
                "reasons": [],
            }

        figures = chunk_index.get("figures") if isinstance(chunk_index.get("figures"), list) else []
        page_assets = chunk_index.get("assets") if isinstance(chunk_index.get("assets"), list) else []
        markdown = str(getattr(parsed, "markdown", "") or "")
        text_lengths: list[int] = []
        visual_unit_count = 0
        unit_number_key = "slide_number" if unit_kind == "slides" else "page_number"
        asset_number_key = "page_number"
        for unit in units:
            try:
                start_char = int(unit.get("start_char"))
                end_char = int(unit.get("end_char"))
            except (TypeError, ValueError):
                start_char = 0
                end_char = 0
            segment = markdown[start_char:end_char] if end_char > start_char else ""
            text_lengths.append(len(self._strip_structural_markers(segment)))
            try:
                unit_number = int(unit.get(unit_number_key))
            except (TypeError, ValueError):
                unit_number = None
            if unit_number is None:
                continue
            has_visual = any(
                isinstance(entry, dict) and int(entry.get(asset_number_key) or 0) == unit_number
                for entry in figures
            ) or any(
                isinstance(entry, dict)
                and int(entry.get(asset_number_key) or 0) == unit_number
                and self._safe_text(entry.get("kind")).startswith(("figure_", "page_image"))
                for entry in page_assets
            )
            if has_visual:
                visual_unit_count += 1

        unit_count = len(units)
        avg_text_chars = sum(text_lengths) / unit_count if unit_count else 0.0
        low_text_unit_count = sum(
            1 for length in text_lengths if length <= self.config.image_heavy_low_text_chars_per_unit
        )
        low_text_ratio = low_text_unit_count / unit_count if unit_count else 0.0
        visual_unit_ratio = visual_unit_count / unit_count if unit_count else 0.0
        chunk_count = int(chunk_index.get("chunk_count") or 0)
        chunk_density = chunk_count / unit_count if unit_count else 0.0

        reasons: list[str] = []
        should_escalate = False
        if (
            low_text_ratio >= self.config.image_heavy_low_text_unit_ratio_threshold
            and visual_unit_ratio >= self.config.image_heavy_visual_unit_ratio_threshold
        ):
            should_escalate = True
            reasons.append("image-dominant office pages/slides with weak extracted text")
        elif (
            low_text_ratio >= self.config.image_heavy_low_text_unit_ratio_threshold
            and avg_text_chars <= self.config.image_heavy_very_low_avg_text_chars_threshold
        ):
            should_escalate = True
            reasons.append("very low text coverage across pages/slides")
        elif (
            low_text_ratio >= self.config.image_heavy_low_text_unit_ratio_threshold
            and chunk_density <= self.config.image_heavy_chunk_density_threshold
            and avg_text_chars <= self.config.image_heavy_low_text_chars_per_unit
        ):
            should_escalate = True
            reasons.append("weak structural extraction across the document")

        return {
            "unit_kind": unit_kind,
            "unit_count": unit_count,
            "avg_text_chars_per_unit": round(avg_text_chars, 2),
            "low_text_unit_count": low_text_unit_count,
            "low_text_unit_ratio": round(low_text_ratio, 4),
            "visual_unit_count": visual_unit_count,
            "visual_unit_ratio": round(visual_unit_ratio, 4),
            "chunk_density": round(chunk_density, 4),
            "should_escalate": should_escalate,
            "reasons": reasons,
        }

    def _strip_structural_markers(self, text: str) -> str:
        without_markers = re.sub(r"(?m)^\[(?:PAGE|SLIDE|FIGURE|TABLE)\b[^\]]*\]\s*$", "", text)
        normalized = re.sub(r"\s+", " ", without_markers).strip()
        return normalized

    def _normalize_input_artifacts(self, raw_artifacts: list[dict[str, Any]]) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        for item in raw_artifacts:
            if not is_supported_document_artifact(item):
                continue
            artifact_id = self._safe_text(item.get("artifact_id")) or self._safe_text(item.get("id"))
            raw_path = self._safe_text(item.get("path"))
            mime = self._safe_text(item.get("mime")) or self._safe_text(item.get("mime_type"))
            filename = self._safe_text(item.get("filename"))
            sha256 = self._safe_text(item.get("sha256"))
            if not artifact_id or not raw_path:
                continue
            candidate_name = filename or Path(raw_path).name
            ext = Path(candidate_name).suffix.lower()
            normalized.append(
                {
                    "artifact_id": artifact_id,
                    "path": raw_path,
                    "mime": mime or infer_document_mime_from_extension(ext),
                    "filename": candidate_name,
                    "sha256": sha256 or "",
                }
            )
        return normalized

    def _resolve_input_artifact_path(self, artifact: dict[str, str]) -> Path:
        raw_path = Path(artifact["path"]).expanduser()
        if raw_path.is_absolute():
            return raw_path.resolve()
        if len(raw_path.parts) >= 2 and raw_path.parts[0] == "runs" and raw_path.parts[1] == "artifacts":
            backend_root = self.artifacts_root.parent.parent
            return (backend_root / raw_path).resolve()
        return (self.artifacts_root / raw_path).resolve()

    def _verify_source_file(self, *, source_path: Path, artifact: dict[str, str]) -> Path:
        if not source_path.exists() or not source_path.is_file():
            raise DocsParserAgentError(
                code="MISSING_ARTIFACT",
                message=f"Input artifact file not found: {artifact['artifact_id']}",
                retryable=False,
                next_action="escalate",
            )
        try:
            source_path.relative_to(self.artifacts_root)
        except ValueError as exc:
            raise DocsParserAgentError(
                code="MISSING_ARTIFACT",
                message=f"Input artifact path is outside the allowed artifacts root: {artifact['artifact_id']}",
                retryable=False,
                next_action="escalate",
            ) from exc
        size_bytes = source_path.stat().st_size
        if size_bytes > self.config.max_input_file_bytes:
            raise DocsParserAgentError(
                code="INVALID_INPUT",
                message=(
                    f"Input artifact {artifact['artifact_id']} exceeds the "
                    f"{self._format_size_limit(self.config.max_input_file_bytes)} docs parsing limit."
                ),
                retryable=False,
                next_action="revise_input",
            )
        expected_sha = artifact.get("sha256") or ""
        if expected_sha:
            digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
            if digest != expected_sha:
                raise DocsParserAgentError(
                    code="MISSING_ARTIFACT",
                    message=f"Input artifact integrity check failed for {artifact['artifact_id']}.",
                    retryable=False,
                    next_action="escalate",
                )
        return source_path

    def _classify_parse_failure(self, message: str) -> tuple[str, bool, str]:
        normalized = message.strip().lower()
        if "docling is not installed" in normalized:
            return "PARSER_UNAVAILABLE", True, "escalate"
        if any(
            marker in normalized
            for marker in (
                "max_num_pages",
                "maximum number of pages",
                "page limit",
                "too many pages",
                "max_file_size",
                "file size limit",
                "exceeds the size limit",
            )
        ):
            return "INVALID_INPUT", False, "revise_input"
        return "PARSE_FAILED", False, "escalate"

    def _format_size_limit(self, size_bytes: int) -> str:
        if size_bytes >= 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.0f} MB"
        if size_bytes >= 1024:
            return f"{max(1, round(size_bytes / 1024))} KB"
        return f"{max(1, size_bytes)} B"

    def _persist_parsed_bundle(
        self,
        *,
        task: TaskEnvelope,
        artifact: dict[str, str],
        doc_id: str,
        bundle_id: str,
        source_path: Path,
        parsed,
        parse_request: ParseRequest,
        enrichment_status: dict[str, Any],
        rendered_office_document: RenderedOfficeDocument | None,
    ) -> tuple[dict[str, Any], list[ArtifactManifest]]:
        bundle_root = self.artifacts_root / task.task_id / "docs_parser" / artifact["artifact_id"]
        bundle_root.mkdir(parents=True, exist_ok=True)

        document_json_path = bundle_root / "document.json"
        document_md_path = bundle_root / "document.md"
        chunk_index_path = bundle_root / "chunk_index.json"
        manifest_path = bundle_root / "manifest.json"
        assets_root = bundle_root / "assets"
        intermediate_root = bundle_root / "intermediate"

        asset_file_paths: dict[str, Path] = {}
        for relative_path, raw_bytes, _mime in parsed.asset_files:
            target_path = bundle_root / Path(relative_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(raw_bytes)
            asset_file_paths[relative_path.replace("\\", "/")] = target_path

        rendered_pdf_path: Path | None = None
        if rendered_office_document is not None:
            intermediate_root.mkdir(parents=True, exist_ok=True)
            rendered_pdf_path = intermediate_root / "rendered_source.pdf"
            rendered_pdf_path.write_bytes(rendered_office_document.rendered_pdf_path.read_bytes())

        normalized_chunk_index = self._normalize_chunk_index_asset_paths(
            parsed.chunk_index,
            bundle_root=bundle_root,
            asset_file_paths=asset_file_paths,
        )

        document_payload = dict(parsed.document_json)
        document_payload.setdefault("doc_id", doc_id)
        document_payload.setdefault("bundle_id", bundle_id)
        document_payload.setdefault(
            "source_artifact",
            {
                "artifact_id": artifact["artifact_id"],
                "filename": artifact["filename"],
                "mime": artifact["mime"],
                "path": self._logical_artifact_path(source_path),
            },
        )
        document_payload.setdefault("parse_request", asdict(parse_request))
        document_payload.setdefault("visual_enrichment", enrichment_status)

        manifest_payload = {
            "doc_id": doc_id,
            "bundle_id": bundle_id,
            "source_artifact_id": artifact["artifact_id"],
            "filename": artifact["filename"],
            "mime": artifact["mime"],
            "title": parsed.title,
            "outputs": {
                "document_json": "document.json",
                "document_md": "document.md",
                "chunk_index": "chunk_index.json",
                "assets_root": "assets",
                "intermediate_root": "intermediate" if intermediate_root.exists() else None,
            },
            "counts": {
                "section_count": parsed.section_count,
                "chunk_count": int(normalized_chunk_index.get("chunk_count") or 0),
                "table_count": parsed.table_count,
                "figure_count": parsed.figure_count,
                "page_count": parsed.page_count,
                "slide_count": parsed.slide_count,
                "asset_count": int(normalized_chunk_index.get("asset_count") or 0),
            },
            "visual_enrichment": enrichment_status,
        }

        document_json_path.write_text(json.dumps(document_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        document_md_path.write_text(parsed.markdown, encoding="utf-8")
        chunk_index_path.write_text(json.dumps(normalized_chunk_index, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest_path.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2), encoding="utf-8")

        artifacts = [
            self._artifact_manifest(task.task_id, document_json_path, "application/json"),
            self._artifact_manifest(task.task_id, document_md_path, "text/markdown"),
            self._artifact_manifest(task.task_id, chunk_index_path, "application/json"),
            self._artifact_manifest(task.task_id, manifest_path, "application/json"),
        ]
        if rendered_pdf_path is not None and rendered_pdf_path.exists():
            artifacts.append(self._artifact_manifest(task.task_id, rendered_pdf_path, "application/pdf"))
        for relative_path, target_path in asset_file_paths.items():
            mime = self._infer_asset_mime(target_path)
            artifacts.append(self._artifact_manifest(task.task_id, target_path, mime))

        summary = {
            "doc_id": doc_id,
            "artifact_id": artifact["artifact_id"],
            "filename": artifact["filename"],
            "mime": artifact["mime"],
            "title": parsed.title,
            "section_count": parsed.section_count,
            "chunk_count": int(normalized_chunk_index.get("chunk_count") or 0),
            "table_count": parsed.table_count,
            "figure_count": parsed.figure_count,
            "page_count": parsed.page_count,
            "slide_count": parsed.slide_count,
            "asset_count": int(normalized_chunk_index.get("asset_count") or 0),
            "visual_enrichment": enrichment_status,
            "artifact_refs": [item.artifact_id for item in artifacts],
            "paths": {
                "manifest": self._logical_artifact_path(manifest_path),
                "document_json": self._logical_artifact_path(document_json_path),
                "document_md": self._logical_artifact_path(document_md_path),
                "chunk_index": self._logical_artifact_path(chunk_index_path),
                "assets_root": self._logical_artifact_path(assets_root) if assets_root.exists() else None,
                "rendered_source_pdf": self._logical_artifact_path(rendered_pdf_path) if rendered_pdf_path else None,
            },
        }
        return summary, artifacts

    def _artifact_manifest(self, task_id: str, file_path: Path, mime: str) -> ArtifactManifest:
        logical_path = self._logical_artifact_path(file_path)
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        return ArtifactManifest(
            artifact_id=f"art_{hashlib.sha256(logical_path.encode('utf-8')).hexdigest()[:16]}",
            task_id=task_id,
            mime=mime,
            sha256=digest,
            path=logical_path,
            created_by_agent=self.agent_id,
            kind="output",
        )

    def _logical_artifact_path(self, file_path: Path) -> str:
        resolved = file_path.resolve()
        try:
            relative_to_artifacts = resolved.relative_to(self.artifacts_root.resolve())
            return (Path("runs") / "artifacts" / relative_to_artifacts).as_posix()
        except ValueError:
            return resolved.relative_to(BACKEND_ROOT.resolve()).as_posix()

    def _normalize_chunk_index_asset_paths(
        self,
        chunk_index: dict[str, Any],
        *,
        bundle_root: Path,
        asset_file_paths: dict[str, Path],
    ) -> dict[str, Any]:
        normalized = json.loads(json.dumps(chunk_index, ensure_ascii=False))
        for key in ("pages", "slides", "tables", "figures", "assets"):
            entries = normalized.get(key)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                raw_path = self._safe_text(entry.get("path"))
                if not raw_path:
                    continue
                candidate = asset_file_paths.get(raw_path.replace("\\", "/"))
                if candidate is None:
                    candidate = bundle_root / raw_path
                if candidate.exists():
                    entry["path"] = self._logical_artifact_path(candidate)
        return normalized

    def _infer_asset_mime(self, file_path: Path) -> str:
        suffix = file_path.suffix.lower()
        if suffix == ".png":
            return "image/png"
        if suffix == ".md":
            return "text/markdown"
        if suffix == ".html":
            return "text/html"
        if suffix == ".json":
            return "application/json"
        return "application/octet-stream"

    def _record_session_run(
        self,
        *,
        task: TaskEnvelope,
        bundle_id: str,
        summary: dict[str, Any],
        artifacts: list[ArtifactManifest],
    ) -> None:
        session_id = self._safe_text(task.session_id) or self._safe_text(task.task_list_id) or "sessionless"
        artifact_refs = [
            {
                "artifact_id": item.artifact_id,
                "path": item.path,
                "mime": item.mime,
            }
            for item in artifacts
        ]
        with connect_sync(self.session_db_path) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO docs_parser_session_runs (
                    task_id,
                    session_id,
                    bundle_id,
                    summary_json,
                    artifact_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    session_id,
                    bundle_id,
                    json.dumps(summary, ensure_ascii=False),
                    json.dumps(artifact_refs, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                ),
            )
            connection.commit()

    def _initialize_store(self) -> None:
        with connect_sync(self.session_db_path) as connection:
            connection.executescript(_RUNS_TABLE_SQL)
            connection.commit()

    def _result_error(self, *, code: str, message: str, retryable: bool, next_action: str) -> AgentResult:
        return AgentResult(
            status="failed",
            output={},
            artifacts=[],
            error=AgentError(code=code, retryable=retryable, message=message, next_action=next_action),
        )

    async def _emit_progress(self, task_id: str, message: str) -> None:
        await self.emit_event(task_id, "task.progress", {"message": message})

    def _coerce_bool(self, value: Any, *, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return default

    def _safe_text(self, value: Any) -> str:
        return str(value).strip() if value is not None else ""

    def _stable_id(self, raw: str) -> str:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _require_bundle_id(self, value: Any) -> str:
        bundle_id = self._safe_text(value)
        if not bundle_id:
            raise DocsParserAgentError(
                code="INVALID_INPUT",
                message="bundle_id is required.",
                retryable=False,
                next_action="revise_input",
            )
        return bundle_id

    def _coerce_positive_int(self, value: Any, *, default: int, minimum: int, maximum: int) -> int:
        try:
            if value is None or str(value).strip() == "":
                return default
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            return default
        return max(minimum, min(maximum, parsed))

    def _bounded_excerpt(self, value: Any, *, limit: int) -> str:
        text = " ".join(self._safe_text(value).split())
        if len(text) <= limit:
            return text
        return f"{text[: limit - 3].rstrip()}..."

    def _load_bundle_output(self, bundle_id: str) -> dict[str, Any]:
        with connect_sync(self.session_db_path) as connection:
            row = connection.execute(
                """
                SELECT summary_json
                FROM docs_parser_session_runs
                WHERE bundle_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (bundle_id,),
            ).fetchone()
        if row is None:
            raise DocsParserAgentError(
                code="MISSING_ARTIFACT",
                message=f"Parsed bundle not found: {bundle_id}",
                retryable=False,
                next_action="escalate",
            )
        try:
            payload = json.loads(str(row["summary_json"] or "{}"))
        except json.JSONDecodeError as exc:
            raise DocsParserAgentError(
                code="INTERNAL_ERROR",
                message=f"Stored bundle summary is invalid JSON for {bundle_id}.",
                retryable=False,
                next_action="escalate",
            ) from exc
        if not isinstance(payload, dict):
            raise DocsParserAgentError(
                code="INTERNAL_ERROR",
                message=f"Stored bundle summary is malformed for {bundle_id}.",
                retryable=False,
                next_action="escalate",
            )
        return payload

    def _select_document_for_read(self, documents: list[Any], *, doc_id: str) -> dict[str, Any]:
        normalized_documents = [item for item in documents if isinstance(item, dict)]
        if not normalized_documents:
            raise DocsParserAgentError(
                code="MISSING_ARTIFACT",
                message="Parsed bundle does not contain any readable documents.",
                retryable=False,
                next_action="escalate",
            )
        if doc_id:
            for item in normalized_documents:
                if self._safe_text(item.get("doc_id")) == doc_id:
                    return item
            raise DocsParserAgentError(
                code="INVALID_INPUT",
                message=f"doc_id was not found in the parsed bundle: {doc_id}",
                retryable=False,
                next_action="revise_input",
            )
        if len(normalized_documents) == 1:
            return normalized_documents[0]
        raise DocsParserAgentError(
            code="INVALID_INPUT",
            message="doc_id is required when a parsed bundle contains multiple documents.",
            retryable=False,
            next_action="revise_input",
        )

    def _document_index_payload(self, document: dict[str, Any], chunk_index: dict[str, Any]) -> dict[str, Any]:
        return {
            "doc_id": self._safe_text(document.get("doc_id")),
            "title": self._safe_text(document.get("title")) or None,
            "filename": self._safe_text(document.get("filename")) or None,
            "mime": self._safe_text(document.get("mime")) or None,
            "paths": document.get("paths") if isinstance(document.get("paths"), dict) else {},
            "counts": {
                "section_count": int(chunk_index.get("section_count") or 0),
                "chunk_count": int(chunk_index.get("chunk_count") or 0),
                "page_count": int(chunk_index.get("page_count") or 0),
                "slide_count": int(chunk_index.get("slide_count") or 0),
                "table_count": int(chunk_index.get("table_count") or 0),
                "figure_count": int(chunk_index.get("figure_count") or 0),
                "asset_count": int(chunk_index.get("asset_count") or 0),
            },
            "available_indexes": [
                "documents",
                "sections",
                "pages",
                "slides",
                "chunks",
                "tables",
                "figures",
                "assets",
            ],
            "available_read_kinds": [
                "document",
                "section",
                "page_range",
                "slide_range",
                "chunk_ids",
                "markdown_window",
            ],
        }

    def _browse_document_index(
        self,
        *,
        index_kind: str,
        selected_document: dict[str, Any],
        chunk_index: dict[str, Any],
        limit: int,
    ) -> dict[str, Any]:
        doc_id = self._safe_text(selected_document.get("doc_id"))
        title = selected_document.get("title")
        if index_kind == "sections":
            sections = chunk_index.get("sections") if isinstance(chunk_index.get("sections"), list) else []
            normalized = [
                {
                    "section_id": self._safe_text(item.get("section_id")) or None,
                    "title": self._safe_text(item.get("title")) or None,
                    "level": item.get("level"),
                    "page_numbers": item.get("page_numbers"),
                    "slide_numbers": item.get("slide_numbers"),
                    "start_char": item.get("start_char"),
                    "end_char": item.get("end_char"),
                    "excerpt": self._bounded_excerpt(item.get("text"), limit=320),
                }
                for item in sections[:limit]
                if isinstance(item, dict)
            ]
            return {
                "response": f"Loaded {len(normalized)} section index item(s).",
                "index_kind": "sections",
                "doc_id": doc_id,
                "title": title,
                "section_count": int(chunk_index.get("section_count") or len(sections)),
                "sections": normalized,
            }
        if index_kind == "chunks":
            chunks = chunk_index.get("chunks") if isinstance(chunk_index.get("chunks"), list) else []
            normalized = [
                {
                    "chunk_id": self._safe_text(item.get("chunk_id")) or None,
                    "section_id": self._safe_text(item.get("section_id")) or None,
                    "section_title": self._safe_text(item.get("section_title")) or None,
                    "page_numbers": item.get("page_numbers"),
                    "slide_numbers": item.get("slide_numbers"),
                    "estimated_chars": item.get("estimated_chars"),
                    "prev_chunk_id": self._safe_text(item.get("prev_chunk_id")) or None,
                    "next_chunk_id": self._safe_text(item.get("next_chunk_id")) or None,
                    "excerpt": self._bounded_excerpt(item.get("text"), limit=240),
                }
                for item in chunks[:limit]
                if isinstance(item, dict)
            ]
            return {
                "response": f"Loaded {len(normalized)} chunk index item(s).",
                "index_kind": "chunks",
                "doc_id": doc_id,
                "title": title,
                "chunk_count": int(chunk_index.get("chunk_count") or len(chunks)),
                "chunks": normalized,
            }
        payload_key = index_kind
        items = chunk_index.get(index_kind) if isinstance(chunk_index.get(index_kind), list) else []
        normalized_items = [item for item in items[:limit] if isinstance(item, dict)]
        return {
            "response": f"Loaded {len(normalized_items)} {index_kind} index item(s).",
            "index_kind": index_kind,
            "doc_id": doc_id,
            "title": title,
            f"{index_kind[:-1] if index_kind.endswith('s') else index_kind}_count": len(items),
            payload_key: normalized_items,
        }

    def _search_bundle(
        self,
        bundle_output: dict[str, Any],
        *,
        query: str,
        limit: int,
        search_kind: str,
        doc_ids: list[str],
    ) -> list[dict[str, Any]]:
        normalized_query = " ".join(self._tokenize_terms(query))
        query_tokens = self._tokenize_terms(query)
        if not query_tokens:
            return []
        candidates: list[dict[str, Any]] = []
        allowed_doc_ids = set(doc_ids)
        for document in bundle_output.get("documents", []):
            if not isinstance(document, dict):
                continue
            doc_id = self._safe_text(document.get("doc_id"))
            if allowed_doc_ids and doc_id not in allowed_doc_ids:
                continue
            chunk_index = self._load_json_artifact(document, "chunk_index")
            entries = chunk_index.get("sections") if search_kind == "sections" else chunk_index.get("chunks")
            if not isinstance(entries, list):
                continue
            section_lengths = self._section_length_map(chunk_index)
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                text = self._safe_text(entry.get("text"))
                section_title = self._safe_text(entry.get("title") if search_kind == "sections" else entry.get("section_title"))
                search_text = self._safe_text(entry.get("search_text")) or " ".join(
                    part for part in (self._safe_text(document.get("title")), section_title, text) if part
                )
                candidates.append(
                    {
                        "document": document,
                        "chunk_index": chunk_index,
                        "entry": entry,
                        "doc_id": doc_id,
                        "doc_title": self._safe_text(document.get("title")),
                        "section_title": section_title,
                        "body_text": text,
                        "search_text": search_text,
                        "search_tokens": self._tokenize_terms(search_text),
                        "section_lengths": section_lengths,
                        "search_kind": search_kind,
                    }
                )
        if not candidates:
            return []
        idf_map, average_length = self._compute_query_idf(query_tokens, candidates)
        matches: list[dict[str, Any]] = []
        for candidate in candidates:
            score = self._score_search_candidate(
                normalized_query=normalized_query,
                query_tokens=query_tokens,
                candidate=candidate,
                idf_map=idf_map,
                average_length=average_length,
            )
            if score <= 0:
                continue
            entry = candidate["entry"]
            text = self._safe_text(candidate.get("body_text"))
            match: dict[str, Any] = {
                "doc_id": candidate["doc_id"],
                "title": candidate["doc_title"] or None,
                "score": score,
                "excerpt": self._excerpt_around_query(text or self._safe_text(candidate.get("search_text")), query, query_tokens),
            }
            if search_kind == "sections":
                match: dict[str, Any] = {
                    "doc_id": candidate["doc_id"],
                    "title": candidate["doc_title"] or None,
                    "score": score,
                    "excerpt": self._excerpt_around_query(text or self._safe_text(candidate.get("search_text")), query, query_tokens),
                    "chunk_id": self._safe_text(entry.get("section_id")) or None,
                    "section_id": self._safe_text(entry.get("section_id")) or None,
                    "section_title": self._safe_text(entry.get("title")) or None,
                    "page_numbers": entry.get("page_numbers"),
                    "slide_numbers": entry.get("slide_numbers"),
                    "recommended_read_kind": "section",
                    "recommended_section_id": self._safe_text(entry.get("section_id")) or None,
                    "recommended_chunk_ids": self._recommended_chunk_ids_for_section(
                        chunk_index=candidate["chunk_index"],
                        section_id=self._safe_text(entry.get("section_id")),
                    ),
                }
            else:
                section_id = self._safe_text(entry.get("section_id")) or None
                recommended_chunk_ids = [
                    chunk_id
                    for chunk_id in (
                        self._safe_text(entry.get("prev_chunk_id")) or None,
                        self._safe_text(entry.get("chunk_id")) or None,
                        self._safe_text(entry.get("next_chunk_id")) or None,
                    )
                    if chunk_id
                ]
                match.update(
                    {
                        "chunk_id": self._safe_text(entry.get("chunk_id")),
                        "section_id": section_id,
                        "section_title": self._safe_text(entry.get("section_title")) or None,
                        "page_numbers": entry.get("page_numbers"),
                        "slide_numbers": entry.get("slide_numbers"),
                        "prev_chunk_id": self._safe_text(entry.get("prev_chunk_id")) or None,
                        "next_chunk_id": self._safe_text(entry.get("next_chunk_id")) or None,
                        "recommended_read_kind": "chunk_ids",
                        "recommended_section_id": section_id,
                        "recommended_chunk_ids": recommended_chunk_ids,
                    }
                )
                section_length = candidate["section_lengths"].get(section_id or "", 0)
                if section_id and section_length and section_length <= 6000:
                    match["recommended_read_kind"] = "section"
            matches.append(match)
        matches.sort(
            key=lambda item: (
                int(item.get("score") or 0),
                str(item.get("section_id") or item.get("chunk_id") or ""),
            ),
            reverse=True,
        )
        return matches[:limit]

    def _tokenize_terms(self, value: str) -> list[str]:
        return [token for token in re.split(r"[^a-z0-9]+", value.lower()) if token]

    def _compute_query_idf(self, query_tokens: list[str], candidates: list[dict[str, Any]]) -> tuple[dict[str, float], float]:
        unique_query_tokens = list(dict.fromkeys(query_tokens))
        doc_freq = {token: 0 for token in unique_query_tokens}
        lengths: list[int] = []
        for candidate in candidates:
            token_list = candidate.get("search_tokens")
            if not isinstance(token_list, list):
                token_list = self._tokenize_terms(self._safe_text(candidate.get("search_text")))
                candidate["search_tokens"] = token_list
            lengths.append(len(token_list))
            token_set = set(token_list)
            for token in unique_query_tokens:
                if token in token_set:
                    doc_freq[token] += 1
        total_docs = max(1, len(candidates))
        average_length = sum(lengths) / len(lengths) if lengths else 1.0
        idf_map = {
            token: math.log(1.0 + ((total_docs - doc_freq[token] + 0.5) / (doc_freq[token] + 0.5)))
            for token in unique_query_tokens
        }
        return idf_map, max(1.0, average_length)

    def _score_search_candidate(
        self,
        *,
        normalized_query: str,
        query_tokens: list[str],
        candidate: dict[str, Any],
        idf_map: dict[str, float],
        average_length: float,
    ) -> int:
        search_text = self._safe_text(candidate.get("search_text"))
        body_text = self._safe_text(candidate.get("body_text"))
        doc_title = self._safe_text(candidate.get("doc_title"))
        section_title = self._safe_text(candidate.get("section_title"))
        normalized_search = search_text.lower()
        normalized_body = body_text.lower()
        normalized_title = doc_title.lower()
        normalized_section = section_title.lower()
        token_list = candidate.get("search_tokens")
        if not isinstance(token_list, list):
            token_list = self._tokenize_terms(search_text)
            candidate["search_tokens"] = token_list
        counts = Counter(token_list)
        matched_terms = sum(1 for token in query_tokens if counts.get(token, 0) > 0)
        if matched_terms == 0:
            return 0
        length = max(1, len(token_list))
        bm25 = 0.0
        k1 = 1.5
        b = 0.75
        for token in query_tokens:
            tf = counts.get(token, 0)
            if tf <= 0:
                continue
            idf = idf_map.get(token, 0.0)
            denominator = tf + k1 * (1.0 - b + b * (length / average_length))
            bm25 += idf * ((tf * (k1 + 1.0)) / denominator)
        coverage = matched_terms / max(1, len(query_tokens))
        score = bm25 * 8.0
        if normalized_query and normalized_query in normalized_body:
            score += 16.0
        elif normalized_query and normalized_query in normalized_search:
            score += 12.0
        if normalized_query and normalized_query in normalized_section:
            score += 8.0
        if normalized_query and normalized_query in normalized_title:
            score += 6.0
        score += coverage * 18.0
        score += self._query_proximity_bonus(query_tokens, token_list)
        if search_text and body_text and search_text != body_text:
            score += 2.0
        return max(1, int(round(score * 10)))

    def _query_proximity_bonus(self, query_tokens: list[str], token_list: list[str]) -> float:
        if not query_tokens or not token_list:
            return 0.0
        positions: list[int] = []
        for token in dict.fromkeys(query_tokens):
            try:
                positions.append(token_list.index(token))
            except ValueError:
                return 0.0
        if not positions:
            return 0.0
        spread = max(positions) - min(positions)
        if spread <= max(8, len(query_tokens) * 3):
            return 5.0
        if spread <= max(16, len(query_tokens) * 5):
            return 2.0
        return 0.0

    def _excerpt_around_query(self, text: str, query: str, query_tokens: list[str], *, limit: int = 800) -> str:
        if not text:
            return ""
        compact = " ".join(text.split())
        if len(compact) <= limit:
            return compact
        normalized_compact = compact.lower()
        normalized_query = query.lower().strip()
        anchor = normalized_compact.find(normalized_query) if normalized_query else -1
        if anchor < 0:
            for token in query_tokens:
                anchor = normalized_compact.find(token)
                if anchor >= 0:
                    break
        if anchor < 0:
            return self._bounded_excerpt(compact, limit=limit)
        half = max(120, limit // 2)
        start = max(0, anchor - half)
        end = min(len(compact), start + limit)
        if start > 0:
            prior_space = compact.rfind(" ", 0, start)
            if prior_space > 0:
                start = prior_space + 1
        if end < len(compact):
            next_space = compact.find(" ", end)
            if next_space > 0:
                end = next_space
        excerpt = compact[start:end].strip()
        if start > 0:
            excerpt = f"...{excerpt}"
        if end < len(compact):
            excerpt = f"{excerpt}..."
        return excerpt

    def _section_length_map(self, chunk_index: dict[str, Any]) -> dict[str, int]:
        section_lengths: dict[str, int] = {}
        for section in chunk_index.get("sections", []) if isinstance(chunk_index.get("sections"), list) else []:
            if not isinstance(section, dict):
                continue
            section_id = self._safe_text(section.get("section_id"))
            if not section_id:
                continue
            section_lengths[section_id] = len(self._safe_text(section.get("text")))
        return section_lengths

    def _recommended_chunk_ids_for_section(self, *, chunk_index: dict[str, Any], section_id: str, limit: int = 3) -> list[str]:
        if not section_id:
            return []
        chunk_ids: list[str] = []
        for chunk in chunk_index.get("chunks", []) if isinstance(chunk_index.get("chunks"), list) else []:
            if not isinstance(chunk, dict):
                continue
            if self._safe_text(chunk.get("section_id")) != section_id:
                continue
            chunk_id = self._safe_text(chunk.get("chunk_id"))
            if chunk_id:
                chunk_ids.append(chunk_id)
            if len(chunk_ids) >= limit:
                break
        return chunk_ids

    def _read_bundle_content(
        self,
        document: dict[str, Any],
        *,
        read_kind: str,
        section_id: str,
        chunk_ids: list[str],
        start_page: int,
        end_page: int,
        start_slide: int,
        end_slide: int,
        anchor_id: str,
        offset_chars: int,
        before_chars: int,
        after_chars: int,
        max_chars: int,
    ) -> tuple[str, str, list[dict[str, Any]], dict[str, Any]]:
        chunk_index = self._load_json_artifact(document, "chunk_index")
        markdown = self._load_text_artifact(document, "document_md")
        sections = chunk_index.get("sections") if isinstance(chunk_index.get("sections"), list) else []
        chunks = chunk_index.get("chunks") if isinstance(chunk_index.get("chunks"), list) else []
        pages = chunk_index.get("pages") if isinstance(chunk_index.get("pages"), list) else []
        slides = chunk_index.get("slides") if isinstance(chunk_index.get("slides"), list) else []
        tables = chunk_index.get("tables") if isinstance(chunk_index.get("tables"), list) else []
        figures = chunk_index.get("figures") if isinstance(chunk_index.get("figures"), list) else []
        citations: list[dict[str, Any]] = []
        extra: dict[str, Any] = {}

        if read_kind == "section":
            if not section_id:
                raise DocsParserAgentError(
                    code="INVALID_INPUT",
                    message="section_id is required when read_kind=section.",
                    retryable=False,
                    next_action="revise_input",
                )
            for section in sections:
                if not isinstance(section, dict):
                    continue
                if self._safe_text(section.get("section_id")) != section_id:
                    continue
                text = self._safe_text(section.get("text"))
                if not text:
                    text = self._render_section_from_chunks(chunks, section_id=section_id)
                text = text[:max_chars]
                citations.append(
                    {
                        "doc_id": self._safe_text(document.get("doc_id")),
                        "section_id": section_id,
                        "title": self._safe_text(section.get("title")) or None,
                    }
                )
                return text, "section", citations, extra
            raise DocsParserAgentError(
                code="INVALID_INPUT",
                message=f"section_id was not found in the parsed bundle: {section_id}",
                retryable=False,
                next_action="revise_input",
            )

        if read_kind == "chunk_ids":
            if not chunk_ids:
                raise DocsParserAgentError(
                    code="INVALID_INPUT",
                    message="chunk_ids are required when read_kind=chunk_ids.",
                    retryable=False,
                    next_action="revise_input",
                )
            selected_chunks: list[dict[str, Any]] = []
            chunk_id_set = set(chunk_ids)
            for chunk in chunks:
                if not isinstance(chunk, dict):
                    continue
                if self._safe_text(chunk.get("chunk_id")) in chunk_id_set:
                    selected_chunks.append(chunk)
            if not selected_chunks:
                raise DocsParserAgentError(
                    code="INVALID_INPUT",
                    message="None of the requested chunk_ids were found in the parsed bundle.",
                    retryable=False,
                    next_action="revise_input",
                )
            content = self._render_chunks_from_markdown(markdown=markdown, chunks=selected_chunks, max_chars=max_chars)
            for chunk in sorted(
                selected_chunks,
                key=lambda item: (
                    int(item.get("doc_start_char") or 0) if isinstance(item, dict) else 0,
                    self._safe_text(item.get("chunk_id")) if isinstance(item, dict) else "",
                ),
            ):
                citations.append(
                    {
                        "doc_id": self._safe_text(document.get("doc_id")),
                        "chunk_id": self._safe_text(chunk.get("chunk_id")),
                        "section_id": self._safe_text(chunk.get("section_id")) or None,
                        "section_title": self._safe_text(chunk.get("section_title")) or None,
                    }
                )
            return content, "chunks", citations, {
                "requested_chunk_count": len(chunk_ids),
                "resolved_chunk_count": len(selected_chunks),
            }

        if read_kind == "page_range":
            return self._read_numbered_range(
                markdown=markdown,
                entries=pages,
                start_number=start_page,
                end_number=end_page,
                number_key="page_number",
                mode="page_range",
                doc_id=self._safe_text(document.get("doc_id")),
                max_chars=max_chars,
            )

        if read_kind == "slide_range":
            return self._read_numbered_range(
                markdown=markdown,
                entries=slides,
                start_number=start_slide,
                end_number=end_slide,
                number_key="slide_number",
                mode="slide_range",
                doc_id=self._safe_text(document.get("doc_id")),
                max_chars=max_chars,
            )

        if read_kind == "markdown_window":
            if not anchor_id:
                raise DocsParserAgentError(
                    code="INVALID_INPUT",
                    message="anchor_id is required when read_kind=markdown_window.",
                    retryable=False,
                    next_action="revise_input",
                )
            anchor = self._find_anchor_entry(anchor_id, sections=sections, chunks=chunks, pages=pages, slides=slides, tables=tables, figures=figures)
            if anchor is None:
                raise DocsParserAgentError(
                    code="INVALID_INPUT",
                    message=f"anchor_id was not found in the parsed bundle: {anchor_id}",
                    retryable=False,
                    next_action="revise_input",
                )
            try:
                anchor_start = int(anchor.get("start_char"))
                anchor_end = int(anchor.get("end_char"))
            except (TypeError, ValueError):
                raise DocsParserAgentError(
                    code="INVALID_INPUT",
                    message=f"anchor_id does not have a readable markdown location: {anchor_id}",
                    retryable=False,
                    next_action="revise_input",
                )
            window_start = max(0, anchor_start - before_chars)
            window_end = min(len(markdown), anchor_end + after_chars)
            if window_end - window_start > max_chars:
                window_end = min(len(markdown), window_start + max_chars)
            citations.append(
                {
                    "doc_id": self._safe_text(document.get("doc_id")),
                    "anchor_id": anchor_id,
                    "path": self._document_path(document, "document_md"),
                }
            )
            return markdown[window_start:window_end], "markdown_window", citations, {
                "anchor_id": anchor_id,
                "window_start_char": window_start,
                "window_end_char": window_end,
            }

        if read_kind != "document":
            raise DocsParserAgentError(
                code="INVALID_INPUT",
                message="read_kind must be one of: document, section, page_range, slide_range, chunk_ids, markdown_window.",
                retryable=False,
                next_action="revise_input",
            )

        start = max(0, offset_chars)
        end = min(len(markdown), start + max_chars)
        citations.append(
            {
                "doc_id": self._safe_text(document.get("doc_id")),
                "path": self._document_path(document, "document_md"),
            }
        )
        extra.update(
            {
                "offset_chars": start,
                "end_offset_chars": end,
                "total_chars": len(markdown),
                "has_more": end < len(markdown),
                "next_offset_chars": end if end < len(markdown) else None,
            }
        )
        return markdown[start:end], "document", citations, extra

    def _read_numbered_range(
        self,
        *,
        markdown: str,
        entries: list[Any],
        start_number: int,
        end_number: int,
        number_key: str,
        mode: str,
        doc_id: str,
        max_chars: int,
    ) -> tuple[str, str, list[dict[str, Any]], dict[str, Any]]:
        if end_number < start_number:
            raise DocsParserAgentError(
                code="INVALID_INPUT",
                message=f"{number_key} end must be greater than or equal to start.",
                retryable=False,
                next_action="revise_input",
            )
        selected = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                number = int(entry.get(number_key))
            except (TypeError, ValueError):
                continue
            if start_number <= number <= end_number:
                selected.append(entry)
        if not selected:
            raise DocsParserAgentError(
                code="INVALID_INPUT",
                message=f"Requested {mode} is outside the parsed bundle coverage.",
                retryable=False,
                next_action="revise_input",
            )
        try:
            start_char = int(selected[0].get("start_char"))
            end_char = int(selected[-1].get("end_char"))
        except (TypeError, ValueError):
            raise DocsParserAgentError(
                code="INVALID_INPUT",
                message=f"Requested {mode} is not directly readable from markdown yet.",
                retryable=False,
                next_action="revise_input",
            )
        citations = [{"doc_id": doc_id, number_key: entry.get(number_key)} for entry in selected]
        text = markdown[start_char:end_char][:max_chars]
        return text, mode, citations, {
            f"start_{number_key}": start_number,
            f"end_{number_key}": end_number,
        }

    def _find_anchor_entry(
        self,
        anchor_id: str,
        *,
        sections: list[Any],
        chunks: list[Any],
        pages: list[Any],
        slides: list[Any],
        tables: list[Any],
        figures: list[Any],
    ) -> dict[str, Any] | None:
        candidate_lists = [sections, chunks, pages, slides, tables, figures]
        for entries in candidate_lists:
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                identifiers = {
                    self._safe_text(entry.get("anchor_id")),
                    self._safe_text(entry.get("section_id")),
                    self._safe_text(entry.get("chunk_id")),
                    self._safe_text(entry.get("page_id")),
                    self._safe_text(entry.get("slide_id")),
                    self._safe_text(entry.get("table_id")),
                    self._safe_text(entry.get("figure_id")),
                }
                if anchor_id in identifiers:
                    return entry
        return None

    def _render_section_from_chunks(self, chunks: list[Any], *, section_id: str) -> str:
        rendered_parts: list[str] = []
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            if self._safe_text(chunk.get("section_id")) != section_id:
                continue
            text = self._safe_text(chunk.get("text"))
            if text:
                rendered_parts.append(text)
        return "\n\n".join(rendered_parts)

    def _render_chunks_from_markdown(self, *, markdown: str, chunks: list[dict[str, Any]], max_chars: int) -> str:
        ranges: list[list[int]] = []
        fallback_parts: list[str] = []
        for chunk in sorted(
            (item for item in chunks if isinstance(item, dict)),
            key=lambda item: (
                int(item.get("doc_start_char") or 0),
                self._safe_text(item.get("chunk_id")),
            ),
        ):
            text = self._safe_text(chunk.get("text"))
            if text:
                fallback_parts.append(text)
            try:
                start = int(chunk.get("doc_start_char"))
                end = int(chunk.get("doc_end_char"))
            except (TypeError, ValueError):
                continue
            if end <= start:
                continue
            if ranges and start <= ranges[-1][1]:
                ranges[-1][1] = max(ranges[-1][1], end)
            else:
                ranges.append([start, end])
        if not ranges:
            return "\n\n".join(part[:max_chars] for part in fallback_parts if part)[:max_chars]
        rendered_parts: list[str] = []
        rendered_chars = 0
        for start, end in ranges:
            if rendered_parts and rendered_chars >= max_chars:
                break
            remaining = max_chars - rendered_chars
            if remaining <= 0:
                break
            text = markdown[start:end].strip()
            if not text:
                continue
            rendered = text[:remaining].rstrip()
            if not rendered:
                continue
            rendered_parts.append(rendered)
            rendered_chars += len(rendered) + (2 if len(rendered_parts) > 1 else 0)
        if rendered_parts:
            return "\n\n".join(rendered_parts)
        return self._bounded_excerpt("\n\n".join(fallback_parts), limit=max_chars)

    def _load_json_artifact(self, document: dict[str, Any], key: str) -> dict[str, Any]:
        path = self._document_path(document, key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DocsParserAgentError(
                code="INTERNAL_ERROR",
                message=f"Parsed artifact is invalid JSON: {path.name}",
                retryable=False,
                next_action="escalate",
            ) from exc
        if not isinstance(payload, dict):
            raise DocsParserAgentError(
                code="INTERNAL_ERROR",
                message=f"Parsed artifact is malformed: {path.name}",
                retryable=False,
                next_action="escalate",
            )
        return payload

    def _load_text_artifact(self, document: dict[str, Any], key: str) -> str:
        return self._document_path(document, key).read_text(encoding="utf-8")

    def _resolve_document_path(self, raw_path: str) -> Path:
        candidate = Path(raw_path)
        if candidate.parts[:2] == ("runs", "artifacts"):
            return (self.artifacts_root / Path(*candidate.parts[2:])).resolve()
        return (BACKEND_ROOT / candidate).resolve()

    def _document_path(self, document: dict[str, Any], key: str) -> Path:
        paths = document.get("paths") if isinstance(document.get("paths"), dict) else {}
        raw = self._safe_text(paths.get(key))
        if not raw:
            raise DocsParserAgentError(
                code="MISSING_ARTIFACT",
                message=f"Parsed document is missing the expected artifact path: {key}",
                retryable=False,
                next_action="escalate",
            )
        return self._resolve_document_path(raw)
