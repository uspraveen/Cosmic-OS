from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from shared.agent_runtime import AgentRuntime
from shared.contracts import AgentError, AgentResult, ArtifactManifest, TaskEnvelope
from shared.sqlite_client import connect_sync
from shared import infer_document_mime_from_extension, is_supported_document_artifact

from .config import AGENT_ROOT, BACKEND_ROOT, DocsParserConfig
from .docling_adapter import DoclingAdapter, ParseRequest, PictureDescriptionRequest


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


class DocsParserAgent(AgentRuntime):
    PARSE_BUNDLE_INTENT = "docs.parse_bundle"
    BROWSE_BUNDLE_INTENT = "docs.browse_bundle"
    SEARCH_BUNDLE_INTENT = "docs.search_bundle"
    READ_BUNDLE_INTENT = "docs.read_bundle"
    FETCH_ASSET_INTENT = "docs.fetch_asset"

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

        await self._emit_progress(task.task_id, f"Parsing {len(artifacts)} uploaded document(s).")
        for index, artifact in enumerate(artifacts, start=1):
            source_path = self._resolve_input_artifact_path(artifact)
            source_path = self._verify_source_file(source_path=source_path, artifact=artifact)
            await self._emit_progress(
                task.task_id,
                f"Parsing document {index}/{len(artifacts)}: {artifact.get('filename') or artifact['artifact_id']}.",
            )
            try:
                parsed, enrichment_status = self._parse_with_enrichment_fallback(
                    source_path=source_path,
                    artifact=artifact,
                    request=parse_request,
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
            summary, manifests = self._persist_parsed_bundle(
                task=task,
                artifact=artifact,
                doc_id=doc_id,
                bundle_id=bundle_id,
                source_path=source_path,
                parsed=parsed,
                parse_request=parse_request,
                enrichment_status=enrichment_status,
            )
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
        documents = bundle_output.get("documents") if isinstance(bundle_output.get("documents"), list) else []
        requested_doc_id = self._safe_text(task.input.get("doc_id"))
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
                path = self._safe_text(asset.get("path"))
                logical_path = path or None
                content = None
                mime = self._safe_text(asset.get("mime")) or None
                if path:
                    file_path = self._resolve_document_path(path)
                    if file_path.exists() and file_path.is_file() and mime and mime.startswith("text/"):
                        content = file_path.read_text(encoding="utf-8")[:max_chars]
                return AgentResult(
                    status="completed",
                    output={
                        "response": f"Loaded asset {asset_id} from parsed bundle {bundle_id}.",
                        "bundle_id": bundle_id,
                        "doc_id": self._safe_text(document.get("doc_id")),
                        "asset_id": asset_id,
                        "asset": asset,
                        "figure": self._match_related_entry(figures, "asset_id", asset_id),
                        "table": self._match_related_entry(tables, "asset_id", asset_id),
                        "content": content,
                        "path": logical_path,
                    },
                    artifacts=[],
                    error=None,
                )
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

    def _build_parse_request(self, payload: dict[str, Any]) -> ParseRequest:
        ocr_mode = self._safe_text(payload.get("ocr_mode")) or "auto"
        if ocr_mode not in {"auto", "off", "force"}:
            raise DocsParserAgentError(
                code="INVALID_INPUT",
                message="ocr_mode must be one of: auto, off, force.",
                retryable=False,
                next_action="revise_input",
            )
        enable_ocr = self.config.default_enable_ocr if ocr_mode == "auto" else (ocr_mode == "force")
        picture_description = self._build_picture_description_request()
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
    ) -> tuple[dict[str, Any], list[ArtifactManifest]]:
        bundle_root = self.artifacts_root / task.task_id / "docs_parser" / artifact["artifact_id"]
        bundle_root.mkdir(parents=True, exist_ok=True)

        document_json_path = bundle_root / "document.json"
        document_md_path = bundle_root / "document.md"
        chunk_index_path = bundle_root / "chunk_index.json"
        manifest_path = bundle_root / "manifest.json"
        assets_root = bundle_root / "assets"

        asset_file_paths: dict[str, Path] = {}
        for relative_path, raw_bytes, _mime in parsed.asset_files:
            target_path = bundle_root / Path(relative_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(raw_bytes)
            asset_file_paths[relative_path.replace("\\", "/")] = target_path

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
        query_tokens = [token for token in re.split(r"[^a-z0-9]+", query.lower()) if token]
        matches: list[dict[str, Any]] = []
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
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                text = self._safe_text(entry.get("text"))
                haystack = " ".join(
                    (
                        self._safe_text(document.get("title")),
                        self._safe_text(entry.get("title") if search_kind == "sections" else entry.get("section_title")),
                        text,
                    )
                ).lower()
                score = self._score_text_match(query.lower(), query_tokens, haystack)
                if score <= 0:
                    continue
                match: dict[str, Any] = {
                    "doc_id": doc_id,
                    "title": self._safe_text(document.get("title")) or None,
                    "score": score,
                    "excerpt": text[:800],
                }
                if search_kind == "sections":
                    match.update(
                        {
                            "section_id": self._safe_text(entry.get("section_id")) or None,
                            "section_title": self._safe_text(entry.get("title")) or None,
                            "page_numbers": entry.get("page_numbers"),
                            "slide_numbers": entry.get("slide_numbers"),
                        }
                    )
                else:
                    match.update(
                        {
                            "chunk_id": self._safe_text(entry.get("chunk_id")),
                            "section_id": self._safe_text(entry.get("section_id")) or None,
                            "section_title": self._safe_text(entry.get("section_title")) or None,
                            "page_numbers": entry.get("page_numbers"),
                            "slide_numbers": entry.get("slide_numbers"),
                        }
                    )
                matches.append(match)
        matches.sort(
            key=lambda item: (
                int(item.get("score") or 0),
                str(item.get("section_id") or item.get("chunk_id") or ""),
            ),
            reverse=True,
        )
        return matches[:limit]

    def _score_text_match(self, normalized_query: str, query_tokens: list[str], haystack: str) -> int:
        score = 0
        if normalized_query and normalized_query in haystack:
            score += 12
        for token in query_tokens:
            if token in haystack:
                score += 3
        return score

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
            rendered_parts: list[str] = []
            rendered_chars = 0
            for chunk in selected_chunks:
                text = self._safe_text(chunk.get("text"))
                if not text:
                    continue
                if rendered_parts and rendered_chars >= max_chars:
                    break
                remaining = max_chars - rendered_chars
                rendered = text[:remaining]
                rendered_parts.append(rendered)
                rendered_chars += len(rendered)
                citations.append(
                    {
                        "doc_id": self._safe_text(document.get("doc_id")),
                        "chunk_id": self._safe_text(chunk.get("chunk_id")),
                        "section_id": self._safe_text(chunk.get("section_id")) or None,
                        "section_title": self._safe_text(chunk.get("section_title")) or None,
                    }
                )
            return "\n\n".join(rendered_parts), "chunks", citations, extra

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
