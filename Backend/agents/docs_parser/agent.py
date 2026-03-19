from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from shared.agent_runtime import AgentRuntime
from shared.contracts import AgentError, AgentResult, ArtifactManifest, TaskEnvelope
from shared.sqlite_client import connect_sync
from shared import infer_document_mime_from_extension, is_supported_document_artifact

from .config import AGENT_ROOT, BACKEND_ROOT, DocsParserConfig
from .docling_adapter import DoclingAdapter, ParseRequest


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
                parsed = self.parser.parse_file(
                    file_path=source_path,
                    artifact_id=artifact["artifact_id"],
                    mime_type=artifact["mime"],
                    request=parse_request,
                )
            except RuntimeError as exc:
                text = str(exc)
                code = "PARSER_UNAVAILABLE" if "Docling is not installed" in text else "PARSE_FAILED"
                raise DocsParserAgentError(
                    code=code,
                    message=text,
                    retryable=(code == "PARSER_UNAVAILABLE"),
                    next_action="escalate",
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
        if index_kind not in {"documents", "sections", "chunks"}:
            raise DocsParserAgentError(
                code="INVALID_INPUT",
                message="index_kind must be one of: documents, sections, chunks.",
                retryable=False,
                next_action="revise_input",
            )

        bundle_output = self._load_bundle_output(bundle_id)
        documents = bundle_output.get("documents") if isinstance(bundle_output.get("documents"), list) else []
        if index_kind == "documents":
            normalized_documents = [item for item in documents if isinstance(item, dict)]
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
        if index_kind == "sections":
            sections = chunk_index.get("sections") if isinstance(chunk_index.get("sections"), list) else []
            normalized_sections = [item for item in sections if isinstance(item, dict)]
            return AgentResult(
                status="completed",
                output={
                    "response": f"Loaded {len(normalized_sections)} section index item(s) from parsed bundle {bundle_id}.",
                    "bundle_id": bundle_id,
                    "index_kind": "sections",
                    "doc_id": selected_document["doc_id"],
                    "title": selected_document.get("title"),
                    "sections": normalized_sections,
                },
                artifacts=[],
                error=None,
            )

        chunks = chunk_index.get("chunks") if isinstance(chunk_index.get("chunks"), list) else []
        normalized_chunks = [item for item in chunks if isinstance(item, dict)]
        limit = self._coerce_positive_int(task.input.get("limit"), default=20, minimum=1, maximum=100)
        chunk_summaries = [
            {
                "chunk_id": self._safe_text(item.get("chunk_id")) or None,
                "section_id": self._safe_text(item.get("section_id")) or None,
                "section_title": self._safe_text(item.get("section_title")) or None,
                "estimated_chars": item.get("estimated_chars"),
                "prev_chunk_id": self._safe_text(item.get("prev_chunk_id")) or None,
                "next_chunk_id": self._safe_text(item.get("next_chunk_id")) or None,
                "excerpt": self._bounded_excerpt(item.get("text"), limit=240),
            }
            for item in normalized_chunks[:limit]
        ]
        return AgentResult(
            status="completed",
            output={
                "response": f"Loaded {len(chunk_summaries)} chunk index item(s) from parsed bundle {bundle_id}.",
                "bundle_id": bundle_id,
                "index_kind": "chunks",
                "doc_id": selected_document["doc_id"],
                "title": selected_document.get("title"),
                "chunk_count": len(normalized_chunks),
                "chunks": chunk_summaries,
            },
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
        limit = self._coerce_positive_int(task.input.get("limit"), default=5, minimum=1, maximum=12)
        bundle_output = self._load_bundle_output(bundle_id)
        matches = self._search_bundle(bundle_output, query=query, limit=limit)
        return AgentResult(
            status="completed",
            output={
                "response": f"Found {len(matches)} matching chunk(s) in bundle {bundle_id}.",
                "bundle_id": bundle_id,
                "query": query,
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
        section_id = self._safe_text(task.input.get("section_id"))
        requested_chunk_ids = [
            self._safe_text(item)
            for item in (task.input.get("chunk_ids") if isinstance(task.input.get("chunk_ids"), list) else [])
            if self._safe_text(item)
        ]
        content, mode, citations = self._read_bundle_content(
            selected_document,
            section_id=section_id,
            chunk_ids=requested_chunk_ids,
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
            },
            artifacts=[],
            error=None,
        )

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
        return ParseRequest(
            enable_ocr=enable_ocr,
            generate_page_images=self._coerce_bool(
                payload.get("generate_page_images"),
                default=self.config.default_generate_page_images,
            ),
            generate_picture_images=self._coerce_bool(
                payload.get("generate_picture_images"),
                default=self.config.default_generate_picture_images,
            ),
            max_chunk_chars=self.config.max_chunk_chars,
            chunk_overlap_chars=self.config.chunk_overlap_chars,
        )

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
                message=f"Input artifact {artifact['artifact_id']} exceeds the size limit for docs parsing.",
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
    ) -> tuple[dict[str, Any], list[ArtifactManifest]]:
        bundle_root = self.artifacts_root / task.task_id / "docs_parser" / artifact["artifact_id"]
        bundle_root.mkdir(parents=True, exist_ok=True)

        document_json_path = bundle_root / "document.json"
        document_md_path = bundle_root / "document.md"
        chunk_index_path = bundle_root / "chunk_index.json"
        manifest_path = bundle_root / "manifest.json"

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
            },
            "counts": {
                "section_count": parsed.section_count,
                "chunk_count": int(parsed.chunk_index.get("chunk_count") or 0),
                "table_count": parsed.table_count,
                "figure_count": parsed.figure_count,
                "page_count": parsed.page_count,
                "slide_count": parsed.slide_count,
            },
        }

        document_json_path.write_text(json.dumps(document_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        document_md_path.write_text(parsed.markdown, encoding="utf-8")
        chunk_index_path.write_text(json.dumps(parsed.chunk_index, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest_path.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2), encoding="utf-8")

        artifacts = [
            self._artifact_manifest(task.task_id, document_json_path, "application/json"),
            self._artifact_manifest(task.task_id, document_md_path, "text/markdown"),
            self._artifact_manifest(task.task_id, chunk_index_path, "application/json"),
            self._artifact_manifest(task.task_id, manifest_path, "application/json"),
        ]

        summary = {
            "doc_id": doc_id,
            "artifact_id": artifact["artifact_id"],
            "filename": artifact["filename"],
            "mime": artifact["mime"],
            "title": parsed.title,
            "section_count": parsed.section_count,
            "chunk_count": int(parsed.chunk_index.get("chunk_count") or 0),
            "table_count": parsed.table_count,
            "figure_count": parsed.figure_count,
            "page_count": parsed.page_count,
            "slide_count": parsed.slide_count,
            "artifact_refs": [item.artifact_id for item in artifacts],
            "paths": {
                "manifest": self._logical_artifact_path(manifest_path),
                "document_json": self._logical_artifact_path(document_json_path),
                "document_md": self._logical_artifact_path(document_md_path),
                "chunk_index": self._logical_artifact_path(chunk_index_path),
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

    def _search_bundle(self, bundle_output: dict[str, Any], *, query: str, limit: int) -> list[dict[str, Any]]:
        query_tokens = [token for token in re.split(r"[^a-z0-9]+", query.lower()) if token]
        matches: list[dict[str, Any]] = []
        for document in bundle_output.get("documents", []):
            if not isinstance(document, dict):
                continue
            chunk_index = self._load_json_artifact(document, "chunk_index")
            chunks = chunk_index.get("chunks") if isinstance(chunk_index.get("chunks"), list) else []
            for chunk in chunks:
                if not isinstance(chunk, dict):
                    continue
                text = self._safe_text(chunk.get("text"))
                haystack = " ".join(
                    (
                        self._safe_text(document.get("title")),
                        self._safe_text(chunk.get("section_title")),
                        text,
                    )
                ).lower()
                score = self._score_text_match(query.lower(), query_tokens, haystack)
                if score <= 0:
                    continue
                matches.append(
                    {
                        "doc_id": self._safe_text(document.get("doc_id")),
                        "title": self._safe_text(document.get("title")) or None,
                        "chunk_id": self._safe_text(chunk.get("chunk_id")),
                        "section_id": self._safe_text(chunk.get("section_id")) or None,
                        "section_title": self._safe_text(chunk.get("section_title")) or None,
                        "score": score,
                        "excerpt": text[:600],
                    }
                )
        matches.sort(key=lambda item: (int(item.get("score") or 0), str(item.get("chunk_id") or "")), reverse=True)
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
        section_id: str,
        chunk_ids: list[str],
        max_chars: int,
    ) -> tuple[str, str, list[dict[str, Any]]]:
        chunk_index = self._load_json_artifact(document, "chunk_index")
        sections = chunk_index.get("sections") if isinstance(chunk_index.get("sections"), list) else []
        chunks = chunk_index.get("chunks") if isinstance(chunk_index.get("chunks"), list) else []
        citations: list[dict[str, Any]] = []

        if section_id:
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
                return text, "section", citations
            raise DocsParserAgentError(
                code="INVALID_INPUT",
                message=f"section_id was not found in the parsed bundle: {section_id}",
                retryable=False,
                next_action="revise_input",
            )

        if chunk_ids:
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
            return "\n\n".join(rendered_parts), "chunks", citations

        markdown = self._load_text_artifact(document, "document_md")
        citations.append(
            {
                "doc_id": self._safe_text(document.get("doc_id")),
                "path": self._document_path(document, "document_md"),
            }
        )
        return markdown[:max_chars], "markdown", citations

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
        candidate = Path(raw)
        if candidate.parts[:2] == ("runs", "artifacts"):
            return (self.artifacts_root / Path(*candidate.parts[2:])).resolve()
        return (BACKEND_ROOT / candidate).resolve()
