"""Bounded LangGraph workflow for slide specialist intents.

Flow: analyze_request → [create_plan?] → prepare_assets → build_slides → render_and_validate → [fix?] → finalize → END

Inspired by diagram_graph.py patterns:
- _GraphCtx dataclass (agent + task)
- _bump_round, _step_plan_update, _result_error helpers
- Plan loop with accumulated_artifacts/outputs
- Validation loop with LLM regeneration
- route_after_* conditional edge functions
"""

from __future__ import annotations

import io
import json
import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from shared.contracts import (
    AgentError,
    AgentResult,
    ArtifactManifest,
    TaskEnvelope,
    TaskInProgress,
)

from .config import AGENT_ROOT, BACKEND_ROOT, SlideAgentConfig
from .internal_llm import plan_deck, plan_edit, repair_deck, validate_slide
from .slide_builder import SlideBuilder, export_to_pdf, render_slides_to_png

logger = logging.getLogger(__name__)

AGENT_ID = "cosmic/slide-agent:1.0.0"
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".ppt", ".pptx"}
_DOCUMENT_MIMES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
_ALLOWED_DOC_CONTEXT_INTENTS = {
    "docs.search_bundle",
    "docs.read_bundle",
    "docs.fetch_asset",
    "docs.reinspect_asset",
}
_ALLOWED_DOC_SEARCH_KINDS = {"sections", "chunks"}
_ALLOWED_DOC_READ_KINDS = {
    "document",
    "section",
    "page_range",
    "slide_range",
    "chunk_ids",
    "markdown_window",
}
_EXISTING_ASSET_SOURCE_KINDS = {
    "from_asset",
    "artifact",
    "existing",
    "input_artifact",
    "source_artifact",
}


class SlideWorkflowState(TypedDict, total=False):
    intent: str
    tool_round: int
    max_tool_rounds: int
    next_action: str
    agent_result: AgentResult | None
    # Input
    description: str
    template: str
    source_pptx_path: str
    edit_request: str
    input_data: dict[str, Any]
    source_artifacts: list[dict[str, Any]]
    source_documents: list[dict[str, Any]]
    source_visual_assets: list[dict[str, Any]]
    source_document_context: list[dict[str, Any]]
    source_artifacts_prepared: bool
    # LLM plan
    llm_deck_plan: dict[str, Any]
    llm_edit_operations: list[dict[str, Any]]
    llm_action: str
    llm_steps: list[str]
    # Assets
    prepared_slides: list[dict[str, Any]]
    assets_ready: bool
    # Build
    pptx_path: str
    build_ok: bool
    build_error: str
    built: bool
    # Validation
    validation_results: list[dict[str, Any]]
    validation_pass: bool
    validation_issues: list[str]
    validation_attempts: int
    max_validation_attempts: int
    # Plan (StepPlan)
    plan_active: bool
    plan_step: int | None
    plan_total_steps: int
    plan_steps: list[str]
    accumulated_artifacts: list[Any]
    accumulated_outputs: list[Any]
    doc_context_request_count: int
    max_doc_context_requests: int
    pending_asset_request: dict[str, Any]
    suspended: bool
    resumed: bool
    # Progress
    analyzed: bool
    assets_prepared: bool
    validated: bool
    finalized: bool


@dataclass(frozen=True, slots=True)
class _GraphCtx:
    agent: Any
    task: TaskEnvelope


def _result_error(
    *, code: str, message: str, retryable: bool = False, next_action: str = "escalate"
) -> AgentResult:
    return AgentResult(
        status="failed",
        output={},
        artifacts=[],
        error=AgentError(
            code=code, retryable=retryable, message=message, next_action=next_action
        ),
    )


def _normalize_artifact_descriptor(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    path = str(raw.get("path") or "").strip()
    if not path:
        return None
    artifact_id = str(raw.get("artifact_id") or "").strip() or Path(path).stem
    mime = str(raw.get("mime") or "").strip().lower()
    filename = str(raw.get("filename") or "").strip() or Path(path).name
    normalized: dict[str, Any] = {
        "artifact_id": artifact_id,
        "path": path,
        "mime": mime,
        "filename": filename,
    }
    for key in (
        "sha256",
        "created_by_agent",
        "kind",
        "audience",
        "source_artifact_id",
        "bundle_id",
        "doc_id",
    ):
        value = raw.get(key)
        if value is not None:
            normalized[key] = value
    return normalized


def _merge_artifact_descriptors(*artifact_lists: Any) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for artifact_list in artifact_lists:
        if not isinstance(artifact_list, list):
            continue
        for raw in artifact_list:
            normalized = _normalize_artifact_descriptor(raw)
            if normalized is None:
                continue
            key = (
                str(normalized.get("artifact_id") or "").strip(),
                str(normalized.get("path") or "").strip(),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(normalized)
    return merged


def _collect_task_source_artifacts(task: TaskEnvelope) -> list[dict[str, Any]]:
    source_artifacts = (
        task.input.get("source_artifacts")
        if isinstance(task.input.get("source_artifacts"), list)
        else []
    )
    return _merge_artifact_descriptors(task.input_artifacts, source_artifacts)


def _is_document_artifact(artifact: dict[str, Any]) -> bool:
    mime = str(artifact.get("mime") or "").strip().lower()
    suffix = Path(str(artifact.get("path") or "")).suffix.lower()
    return mime in _DOCUMENT_MIMES or suffix in _DOCUMENT_EXTENSIONS


def _is_image_artifact(artifact: dict[str, Any]) -> bool:
    mime = str(artifact.get("mime") or "").strip().lower()
    suffix = Path(str(artifact.get("path") or "")).suffix.lower()
    return mime.startswith("image/") or suffix in _IMAGE_EXTENSIONS


def _is_docs_parser_artifact(artifact: dict[str, Any]) -> bool:
    path = str(artifact.get("path") or "").replace("\\", "/").lower()
    created_by_agent = str(artifact.get("created_by_agent") or "").strip().lower()
    filename = str(artifact.get("filename") or Path(str(artifact.get("path") or "")).name).strip().lower()
    return (
        "/docs_parser/" in path
        or "docs-parser" in created_by_agent
        or filename in {"manifest.json", "chunk_index.json"}
    )


def _safe_json_load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _resolve_bundle_path(bundle_root: Path, raw_path: str | None) -> Path | None:
    resolved = _resolve_artifact_file(raw_path)
    if resolved is not None:
        return resolved
    relative = str(raw_path or "").strip()
    if not relative:
        return None
    candidate = (bundle_root / relative).resolve()
    return candidate if candidate.exists() and candidate.is_file() else None


def _merge_document_summaries(*document_lists: Any) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    index_by_key: dict[tuple[str, str], int] = {}

    def _merge_summary_fields(
        current: dict[str, Any],
        incoming: dict[str, Any],
    ) -> dict[str, Any]:
        merged_summary = dict(current)
        for key, raw_value in incoming.items():
            value = _json_safe_copy(raw_value)
            if key in {"artifact_refs", "top_sections", "citations"} and isinstance(
                value, list
            ):
                existing = (
                    list(merged_summary.get(key))
                    if isinstance(merged_summary.get(key), list)
                    else []
                )
                seen_items: set[str] = set()
                combined: list[Any] = []
                for item in existing + value:
                    fingerprint = json.dumps(
                        _json_safe_copy(item), ensure_ascii=False, sort_keys=True
                    )
                    if fingerprint in seen_items:
                        continue
                    seen_items.add(fingerprint)
                    combined.append(item)
                merged_summary[key] = combined
                continue
            if key == "paths" and isinstance(value, dict):
                existing_paths = (
                    dict(merged_summary.get(key))
                    if isinstance(merged_summary.get(key), dict)
                    else {}
                )
                for path_key, path_value in value.items():
                    if path_key not in existing_paths or not existing_paths.get(path_key):
                        existing_paths[path_key] = path_value
                merged_summary[key] = existing_paths
                continue
            if key in {
                "section_count",
                "chunk_count",
                "table_count",
                "figure_count",
                "page_count",
                "slide_count",
                "asset_count",
            } and isinstance(value, int):
                current_value = merged_summary.get(key)
                if current_value in {None, "", 0} or (
                    isinstance(current_value, int) and value > current_value
                ):
                    merged_summary[key] = value
                continue
            current_value = merged_summary.get(key)
            if _is_emptyish(current_value) and not _is_emptyish(value):
                merged_summary[key] = value
                continue
            if key not in merged_summary:
                merged_summary[key] = value
        return merged_summary

    for document_list in document_lists:
        if not isinstance(document_list, list):
            continue
        for raw in document_list:
            if not isinstance(raw, dict):
                continue
            artifact_id = str(raw.get("artifact_id") or "").strip()
            doc_id = str(raw.get("doc_id") or "").strip()
            key = (artifact_id, doc_id)
            normalized = _json_safe_copy(raw)
            if key in index_by_key:
                merged[index_by_key[key]] = _merge_summary_fields(
                    merged[index_by_key[key]],
                    normalized,
                )
                continue
            index_by_key[key] = len(merged)
            merged.append(normalized)
    return merged


def _compact_text(value: Any, *, limit: int = 1200) -> str | None:
    text = " ".join(str(value or "").split())
    if not text:
        return None
    if len(text) <= limit:
        return text
    clipped = text[:limit].rsplit(" ", 1)[0].strip()
    return f"{clipped}..." if clipped else f"{text[:limit]}..."


def _is_emptyish(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _top_sections_from_chunk_index(
    chunk_index: dict[str, Any],
    *,
    limit: int = 6,
) -> list[dict[str, Any]]:
    sections = (
        chunk_index.get("sections")
        if isinstance(chunk_index.get("sections"), list)
        else []
    )
    normalized: list[dict[str, Any]] = []
    for entry in sections:
        if not isinstance(entry, dict):
            continue
        section_id = str(entry.get("section_id") or entry.get("id") or "").strip()
        title = str(entry.get("title") or "").strip()
        preview = _compact_text(
            entry.get("summary")
            or entry.get("excerpt")
            or entry.get("text")
            or ""
        )
        if not section_id and not title and not preview:
            continue
        normalized.append(
            {
                "section_id": section_id or None,
                "title": title or None,
                "page_number": entry.get("page_number"),
                "slide_number": entry.get("slide_number"),
                "preview": preview,
            }
        )
        if len(normalized) >= limit:
            break
    return normalized


def _document_summaries_from_artifacts(
    artifacts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for artifact in artifacts:
        resolved = _resolve_artifact_file(str(artifact.get("path") or ""))
        if resolved is None:
            continue
        if resolved.name != "manifest.json":
            continue
        payload = _safe_json_load(resolved)
        if not payload:
            continue
        counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
        outputs = payload.get("outputs") if isinstance(payload.get("outputs"), dict) else {}
        bundle_root = resolved.parent
        document_md_path = _resolve_bundle_path(
            bundle_root, outputs.get("document_md") or "document.md"
        )
        chunk_index_path = _resolve_bundle_path(
            bundle_root, outputs.get("chunk_index") or "chunk_index.json"
        )
        markdown_excerpt = None
        if document_md_path is not None:
            try:
                markdown_excerpt = _compact_text(
                    document_md_path.read_text(encoding="utf-8"),
                    limit=1800,
                )
            except Exception:
                markdown_excerpt = None
        chunk_index = (
            _safe_json_load(chunk_index_path) if chunk_index_path is not None else {}
        )
        summaries.append(
            {
                "doc_id": str(payload.get("doc_id") or "").strip(),
                "bundle_id": str(payload.get("bundle_id") or "").strip() or None,
                "artifact_id": str(payload.get("source_artifact_id") or artifact.get("artifact_id") or "").strip(),
                "filename": str(payload.get("filename") or artifact.get("filename") or "").strip() or None,
                "mime": str(payload.get("mime") or artifact.get("mime") or "").strip().lower(),
                "title": str(payload.get("title") or "").strip() or None,
                "section_count": int(counts.get("section_count") or 0),
                "chunk_count": int(counts.get("chunk_count") or 0),
                "table_count": int(counts.get("table_count") or 0),
                "figure_count": int(counts.get("figure_count") or 0),
                "page_count": counts.get("page_count"),
                "slide_count": counts.get("slide_count"),
                "asset_count": int(counts.get("asset_count") or 0),
                "preview_excerpt": markdown_excerpt,
                "top_sections": _top_sections_from_chunk_index(chunk_index),
                "paths": {
                    "manifest": resolved.as_posix(),
                    "document_md": document_md_path.as_posix()
                    if document_md_path is not None
                    else None,
                    "chunk_index": chunk_index_path.as_posix()
                    if chunk_index_path is not None
                    else None,
                },
                "artifact_refs": [],
            }
        )
    return summaries


def _merge_visual_assets(*asset_lists: Any) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    index_by_key: dict[str, int] = {}
    for asset_list in asset_lists:
        if not isinstance(asset_list, list):
            continue
        for raw in asset_list:
            if not isinstance(raw, dict):
                continue
            normalized = _json_safe_copy(raw)
            key = (
                str(normalized.get("asset_ref") or "").strip()
                or str(normalized.get("artifact_id") or "").strip()
                or str(normalized.get("path") or "").strip()
            )
            if not key:
                continue
            if key in index_by_key:
                current = dict(merged[index_by_key[key]])
                for field, value in normalized.items():
                    if _is_emptyish(current.get(field)) and not _is_emptyish(value):
                        current[field] = value
                    elif field == "reinspection" and isinstance(value, dict):
                        current[field] = value
                merged[index_by_key[key]] = current
                continue
            index_by_key[key] = len(merged)
            merged.append(normalized)
    return merged


def _extract_visual_assets_from_artifacts(
    artifacts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    visual_assets: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    bundle_docs_by_root: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        resolved = _resolve_artifact_file(str(artifact.get("path") or ""))
        if resolved is None or resolved.name != "manifest.json":
            continue
        payload = _safe_json_load(resolved)
        if not payload:
            continue
        bundle_docs_by_root[resolved.parent.as_posix()] = {
            "bundle_id": str(payload.get("bundle_id") or "").strip() or None,
            "doc_id": str(payload.get("doc_id") or "").strip() or None,
            "title": str(payload.get("title") or "").strip() or None,
        }

    def _append(entry: dict[str, Any]) -> None:
        asset_ref = str(entry.get("asset_ref") or "").strip()
        if not asset_ref or asset_ref in seen_refs:
            return
        seen_refs.add(asset_ref)
        visual_assets.append(entry)

    for artifact in artifacts:
        resolved = _resolve_artifact_file(str(artifact.get("path") or ""))
        if resolved is None:
            continue
        if _is_image_artifact(artifact):
            _append(
                {
                    "asset_ref": str(artifact.get("artifact_id") or "").strip() or resolved.stem,
                    "artifact_id": str(artifact.get("artifact_id") or "").strip() or resolved.stem,
                    "kind": "uploaded_image" if not _is_docs_parser_artifact(artifact) else "image_asset",
                    "filename": str(artifact.get("filename") or resolved.name).strip() or resolved.name,
                    "mime": str(artifact.get("mime") or "").strip().lower() or "image/png",
                    "description": None,
                    "classification_label": None,
                    "page_number": None,
                    "slide_number": None,
                    "bundle_id": artifact.get("bundle_id"),
                    "doc_id": artifact.get("doc_id"),
                    "path": resolved.as_posix(),
                }
            )
            continue
        if resolved.name != "chunk_index.json":
            continue
        bundle_root = resolved.parent
        bundle_doc = bundle_docs_by_root.get(bundle_root.as_posix(), {})
        chunk_index = _safe_json_load(resolved)
        if not chunk_index:
            continue
        figures = chunk_index.get("figures") if isinstance(chunk_index.get("figures"), list) else []
        figure_by_asset_id = {
            str(item.get("asset_id") or "").strip(): item
            for item in figures
            if isinstance(item, dict) and str(item.get("asset_id") or "").strip()
        }
        pages = chunk_index.get("pages") if isinstance(chunk_index.get("pages"), list) else []
        page_by_asset_id = {
            str(item.get("asset_id") or "").strip(): item
            for item in pages
            if isinstance(item, dict) and str(item.get("asset_id") or "").strip()
        }
        slides = chunk_index.get("slides") if isinstance(chunk_index.get("slides"), list) else []
        slide_by_asset_id = {
            str(item.get("asset_id") or "").strip(): item
            for item in slides
            if isinstance(item, dict) and str(item.get("asset_id") or "").strip()
        }
        assets = chunk_index.get("assets") if isinstance(chunk_index.get("assets"), list) else []
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            kind = str(asset.get("kind") or "").strip().lower()
            if kind not in {"figure_image", "page_image", "slide_image"}:
                continue
            asset_ref = str(asset.get("asset_id") or "").strip()
            if not asset_ref:
                continue
            asset_path = _resolve_bundle_path(bundle_root, asset.get("path"))
            if asset_path is None:
                continue
            figure = figure_by_asset_id.get(asset_ref) if asset_ref else None
            page = page_by_asset_id.get(asset_ref) if asset_ref else None
            slide = slide_by_asset_id.get(asset_ref) if asset_ref else None
            classification = (
                asset.get("classification")
                if isinstance(asset.get("classification"), dict)
                else (figure.get("classification") if isinstance(figure, dict) and isinstance(figure.get("classification"), dict) else {})
            )
            _append(
                {
                    "asset_ref": asset_ref,
                    "artifact_id": asset_ref,
                    "kind": kind,
                    "filename": asset_path.name,
                    "mime": str(asset.get("mime") or "").strip().lower() or "image/png",
                    "description": str(
                        asset.get("description")
                        or (figure.get("description") if isinstance(figure, dict) else "")
                        or ""
                    ).strip()
                    or None,
                    "caption": str(
                        (figure.get("caption") if isinstance(figure, dict) else "")
                        or ""
                    ).strip()
                    or None,
                    "classification_label": str(classification.get("label") or "").strip() or None,
                    "page_number": (
                        asset.get("page_number")
                        if asset.get("page_number") is not None
                        else (figure.get("page_number") if isinstance(figure, dict) else page.get("page_number") if isinstance(page, dict) else None)
                    ),
                    "slide_number": (
                        asset.get("slide_number")
                        if asset.get("slide_number") is not None
                        else (figure.get("slide_number") if isinstance(figure, dict) else slide.get("slide_number") if isinstance(slide, dict) else None)
                    ),
                    "bundle_id": bundle_doc.get("bundle_id"),
                    "doc_id": bundle_doc.get("doc_id"),
                    "document_title": bundle_doc.get("title"),
                    "path": asset_path.as_posix(),
                }
            )
    return visual_assets


def _build_source_material_prompt_payload(
    *,
    documents: list[dict[str, Any]],
    visual_assets: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "documents": [
            {
                "doc_id": str(item.get("doc_id") or "").strip() or None,
                "bundle_id": str(item.get("bundle_id") or "").strip() or None,
                "artifact_id": str(item.get("artifact_id") or "").strip() or None,
                "filename": item.get("filename"),
                "title": item.get("title"),
                "mime": item.get("mime"),
                "figure_count": item.get("figure_count"),
                "page_count": item.get("page_count"),
                "slide_count": item.get("slide_count"),
                "preview_excerpt": item.get("preview_excerpt"),
                "top_sections": item.get("top_sections")[:6]
                if isinstance(item.get("top_sections"), list)
                else [],
            }
            for item in documents[:8]
        ],
        "visual_assets": [
            {
                "asset_ref": str(item.get("asset_ref") or "").strip(),
                "bundle_id": str(item.get("bundle_id") or "").strip() or None,
                "doc_id": str(item.get("doc_id") or "").strip() or None,
                "kind": item.get("kind"),
                "filename": item.get("filename"),
                "description": item.get("description"),
                "caption": item.get("caption"),
                "classification_label": item.get("classification_label"),
                "page_number": item.get("page_number"),
                "slide_number": item.get("slide_number"),
            }
            for item in visual_assets[:32]
        ],
    }


def _document_context_key(item: dict[str, Any]) -> str:
    kind = str(item.get("kind") or "").strip()
    if kind == "search_hits":
        return "|".join(
            [
                kind,
                str(item.get("bundle_id") or "").strip(),
                str(item.get("doc_id") or "").strip(),
                str(item.get("query") or "").strip().lower(),
            ]
        )
    if kind == "read_excerpt":
        return "|".join(
            [
                kind,
                str(item.get("bundle_id") or "").strip(),
                str(item.get("doc_id") or "").strip(),
                str(item.get("mode") or "").strip(),
                str(item.get("section_id") or "").strip(),
            ]
        )
    if kind in {"asset_lookup", "visual_reinspection"}:
        return "|".join(
            [
                kind,
                str(item.get("bundle_id") or "").strip(),
                str(item.get("asset_ref") or "").strip(),
                str(item.get("question") or "").strip().lower(),
            ]
        )
    return json.dumps(_json_safe_copy(item), ensure_ascii=False, sort_keys=True)


def _merge_document_context_items(*context_lists: Any) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    index_by_key: dict[str, int] = {}
    for context_list in context_lists:
        if not isinstance(context_list, list):
            continue
        for raw in context_list:
            if not isinstance(raw, dict):
                continue
            normalized = _json_safe_copy(raw)
            key = _document_context_key(normalized)
            if key in index_by_key:
                current = dict(merged[index_by_key[key]])
                for field, value in normalized.items():
                    if _is_emptyish(current.get(field)) and not _is_emptyish(value):
                        current[field] = value
                merged[index_by_key[key]] = current
                continue
            index_by_key[key] = len(merged)
            merged.append(normalized)
    return merged


def _build_document_context_prompt_payload(
    document_context: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "items": [
            {
                "kind": str(item.get("kind") or "").strip() or None,
                "bundle_id": str(item.get("bundle_id") or "").strip() or None,
                "doc_id": str(item.get("doc_id") or "").strip() or None,
                "title": item.get("title"),
                "query": item.get("query"),
                "search_kind": item.get("search_kind"),
                "mode": item.get("mode"),
                "section_id": item.get("section_id"),
                "content_excerpt": item.get("content_excerpt"),
                "matches": item.get("matches"),
                "asset_ref": item.get("asset_ref"),
                "asset_kind": item.get("asset_kind"),
                "analysis_summary": item.get("analysis_summary"),
                "visible_text": item.get("visible_text"),
                "question": item.get("question"),
            }
            for item in document_context[:10]
        ]
    }


def _resolve_bundle_doc_ids(
    bundle_id: str,
    source_documents: list[dict[str, Any]],
) -> list[str]:
    doc_ids: list[str] = []
    for item in source_documents:
        if str(item.get("bundle_id") or "").strip() != bundle_id:
            continue
        doc_id = str(item.get("doc_id") or "").strip()
        if doc_id and doc_id not in doc_ids:
            doc_ids.append(doc_id)
    return doc_ids


def _resolve_doc_context_target_intent(raw_intent: Any) -> str | None:
    text = str(raw_intent or "").strip().lower()
    if not text:
        return None
    if not text.startswith("docs."):
        text = f"docs.{text}"
    return text if text in _ALLOWED_DOC_CONTEXT_INTENTS else None


def _normalize_doc_context_request(
    raw_request: Any,
    *,
    source_documents: list[dict[str, Any]],
    source_visual_assets: list[dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(raw_request, dict):
        raise RuntimeError(
            "Slide LLM requested doc context without a structured doc_request object."
        )

    target_intent = _resolve_doc_context_target_intent(
        raw_request.get("target_intent") or raw_request.get("intent")
    )
    if not target_intent:
        raise RuntimeError(
            "Slide LLM requested an unsupported docs intent. Use one of docs.search_bundle, docs.read_bundle, docs.fetch_asset, docs.reinspect_asset."
        )

    bundle_id = str(raw_request.get("bundle_id") or "").strip()
    if not bundle_id:
        raise RuntimeError("Slide doc context requests must include bundle_id.")

    known_bundle_ids = {
        str(item.get("bundle_id") or "").strip()
        for item in source_documents
        if str(item.get("bundle_id") or "").strip()
    } | {
        str(item.get("bundle_id") or "").strip()
        for item in source_visual_assets
        if str(item.get("bundle_id") or "").strip()
    }
    if bundle_id not in known_bundle_ids:
        raise RuntimeError(
            f"Slide doc context request referenced unknown bundle_id '{bundle_id}'."
        )

    known_doc_ids = _resolve_bundle_doc_ids(bundle_id, source_documents)
    doc_id = str(raw_request.get("doc_id") or "").strip()
    if doc_id and known_doc_ids and doc_id not in known_doc_ids:
        raise RuntimeError(
            f"Slide doc context request referenced unknown doc_id '{doc_id}' for bundle {bundle_id}."
        )
    if not doc_id and len(known_doc_ids) == 1:
        doc_id = known_doc_ids[0]

    target_input: dict[str, Any] = {"bundle_id": bundle_id}
    reason = _compact_text(raw_request.get("reason") or raw_request.get("query"), limit=240)

    if target_intent == "docs.search_bundle":
        query = str(raw_request.get("query") or "").strip()
        if not query:
            raise RuntimeError("docs.search_bundle requires a non-empty query.")
        target_input["query"] = query[:1000]
        search_kind = str(raw_request.get("search_kind") or "").strip().lower()
        if search_kind:
            if search_kind not in _ALLOWED_DOC_SEARCH_KINDS:
                raise RuntimeError(
                    "docs.search_bundle search_kind must be 'sections' or 'chunks'."
                )
            target_input["search_kind"] = search_kind
        if doc_id:
            target_input["doc_ids"] = [doc_id]
        elif isinstance(raw_request.get("doc_ids"), list):
            requested_doc_ids = [
                str(item).strip()
                for item in raw_request.get("doc_ids")
                if str(item).strip() and str(item).strip() in known_doc_ids
            ][:12]
            if requested_doc_ids:
                target_input["doc_ids"] = requested_doc_ids
        limit = raw_request.get("limit")
        if isinstance(limit, int):
            target_input["limit"] = min(12, max(1, limit))
    elif target_intent == "docs.read_bundle":
        if not doc_id and len(known_doc_ids) > 1:
            raise RuntimeError(
                "docs.read_bundle requires doc_id when the source bundle contains multiple documents."
            )
        if doc_id:
            target_input["doc_id"] = doc_id
        read_kind = str(raw_request.get("read_kind") or "").strip().lower()
        if read_kind:
            if read_kind not in _ALLOWED_DOC_READ_KINDS:
                raise RuntimeError(
                    "docs.read_bundle read_kind must be one of document, section, page_range, slide_range, chunk_ids, markdown_window."
                )
            target_input["read_kind"] = read_kind
        for key in (
            "section_id",
            "anchor_id",
        ):
            value = str(raw_request.get(key) or "").strip()
            if value:
                target_input[key] = value
        chunk_ids = raw_request.get("chunk_ids")
        if isinstance(chunk_ids, list):
            normalized_chunk_ids = [
                str(item).strip() for item in chunk_ids if str(item).strip()
            ][:8]
            if normalized_chunk_ids:
                target_input["chunk_ids"] = normalized_chunk_ids
        for key in (
            "start_page",
            "end_page",
            "start_slide",
            "end_slide",
            "offset_chars",
            "before_chars",
            "after_chars",
            "max_chars",
        ):
            value = raw_request.get(key)
            if isinstance(value, int):
                target_input[key] = value
        target_input.setdefault("max_chars", 4000)
    else:
        asset_id = str(
            raw_request.get("asset_id")
            or raw_request.get("asset_ref")
            or ""
        ).strip()
        if not asset_id:
            raise RuntimeError(f"{target_intent} requires asset_id or asset_ref.")
        matching_asset = next(
            (
                item
                for item in source_visual_assets
                if str(item.get("bundle_id") or "").strip() in {"", bundle_id}
                if asset_id
                in {
                    str(item.get("asset_ref") or "").strip(),
                    str(item.get("artifact_id") or "").strip(),
                }
            ),
            None,
        )
        if matching_asset is None:
            raise RuntimeError(
                f"{target_intent} referenced unknown asset '{asset_id}'."
            )
        target_input["asset_id"] = (
            str(matching_asset.get("asset_ref") or "").strip() or asset_id
        )
        asset_doc_id = str(matching_asset.get("doc_id") or "").strip()
        if doc_id:
            target_input["doc_id"] = doc_id
        elif asset_doc_id:
            target_input["doc_id"] = asset_doc_id
        if target_intent == "docs.reinspect_asset":
            question = str(raw_request.get("question") or "").strip()
            if question:
                target_input["question"] = question[:500]

    return {
        "target_intent": target_intent,
        "target_input": target_input,
        "reason": reason
        or f"slide_agent needs deeper document context via {target_intent}",
    }


def _visual_asset_from_docs_output(output: dict[str, Any]) -> dict[str, Any] | None:
    asset = output.get("asset") if isinstance(output.get("asset"), dict) else {}
    if not asset:
        return None
    asset_path = str(output.get("path") or asset.get("path") or "").strip()
    resolved_path = _resolve_artifact_file(asset_path)
    mime = str(asset.get("mime") or "").strip().lower()
    suffix = Path(asset_path).suffix.lower()
    if resolved_path is None or not (mime.startswith("image/") or suffix in _IMAGE_EXTENSIONS):
        return None
    figure = output.get("figure") if isinstance(output.get("figure"), dict) else {}
    classification = (
        asset.get("classification")
        if isinstance(asset.get("classification"), dict)
        else (figure.get("classification") if isinstance(figure.get("classification"), dict) else {})
    )
    analysis = (
        output.get("analysis")
        if isinstance(output.get("analysis"), dict)
        else (
            output.get("reinspection")
            if isinstance(output.get("reinspection"), dict)
            else None
        )
    )
    asset_ref = str(output.get("asset_id") or asset.get("asset_id") or "").strip()
    return {
        "asset_ref": asset_ref or resolved_path.stem,
        "artifact_id": asset_ref or resolved_path.stem,
        "kind": str(asset.get("kind") or "image_asset").strip().lower() or "image_asset",
        "filename": resolved_path.name,
        "mime": mime or "image/png",
        "description": str(
            asset.get("description")
            or figure.get("description")
            or ""
        ).strip()
        or None,
        "caption": str(figure.get("caption") or "").strip() or None,
        "classification_label": str(classification.get("label") or "").strip() or None,
        "page_number": asset.get("page_number") or figure.get("page_number"),
        "slide_number": asset.get("slide_number") or figure.get("slide_number"),
        "bundle_id": str(output.get("bundle_id") or "").strip() or None,
        "doc_id": str(output.get("doc_id") or "").strip() or None,
        "path": resolved_path.as_posix(),
        "reinspection": analysis,
    }


def _apply_docs_context_reverse_result(
    *,
    source_visual_assets: list[dict[str, Any]],
    document_context: list[dict[str, Any]],
    pending_request: dict[str, Any],
    reverse_result: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    status = str(reverse_result.get("status") or "").strip().lower()
    if status and status != "completed":
        err = (
            reverse_result.get("error")
            if isinstance(reverse_result.get("error"), dict)
            else {}
        )
        code = str(err.get("code") or "DOCS_CONTEXT_FAILED").strip()
        message = str(err.get("message") or "Delegated document lookup failed.").strip()
        raise RuntimeError(f"{code}: {message}")

    output = reverse_result.get("output") if isinstance(reverse_result.get("output"), dict) else {}
    target_intent = str(pending_request.get("target_intent") or "").strip().lower()

    context_item: dict[str, Any]
    extra_visual_assets: list[dict[str, Any]] = []
    if target_intent == "docs.search_bundle":
        raw_matches = output.get("matches") if isinstance(output.get("matches"), list) else []
        matches: list[dict[str, Any]] = []
        for match in raw_matches[:5]:
            if not isinstance(match, dict):
                continue
            matches.append(
                {
                    "doc_id": str(match.get("doc_id") or "").strip() or None,
                    "title": match.get("title"),
                    "chunk_id": str(match.get("chunk_id") or "").strip() or None,
                    "section_id": str(match.get("section_id") or "").strip() or None,
                    "section_title": match.get("section_title"),
                    "score": match.get("score"),
                    "excerpt": _compact_text(match.get("excerpt"), limit=600),
                }
            )
        context_item = {
            "kind": "search_hits",
            "bundle_id": str(output.get("bundle_id") or "").strip() or None,
            "doc_id": str(pending_request.get("doc_id") or "").strip() or None,
            "query": str(output.get("query") or "").strip() or None,
            "search_kind": str(output.get("search_kind") or "").strip() or None,
            "matches": matches,
        }
    elif target_intent == "docs.read_bundle":
        citations = output.get("citations") if isinstance(output.get("citations"), list) else []
        context_item = {
            "kind": "read_excerpt",
            "bundle_id": str(output.get("bundle_id") or "").strip() or None,
            "doc_id": str(output.get("doc_id") or "").strip() or None,
            "title": output.get("title"),
            "mode": str(output.get("mode") or "").strip() or None,
            "section_id": str(pending_request.get("section_id") or "").strip() or None,
            "content_excerpt": _compact_text(output.get("content"), limit=4000),
            "citations": citations[:6],
        }
    elif target_intent == "docs.fetch_asset":
        asset_entry = _visual_asset_from_docs_output(output)
        if asset_entry is not None:
            extra_visual_assets.append(asset_entry)
        context_item = {
            "kind": "asset_lookup",
            "bundle_id": str(output.get("bundle_id") or "").strip() or None,
            "doc_id": str(output.get("doc_id") or "").strip() or None,
            "asset_ref": str(output.get("asset_id") or "").strip() or None,
            "asset_kind": (
                output.get("asset", {}).get("kind")
                if isinstance(output.get("asset"), dict)
                else None
            ),
            "content_excerpt": _compact_text(output.get("content"), limit=1500),
            "analysis_summary": _compact_text(
                (
                    output.get("reinspection", {}).get("summary")
                    if isinstance(output.get("reinspection"), dict)
                    else None
                ),
                limit=800,
            ),
        }
    else:
        asset_entry = _visual_asset_from_docs_output(output)
        if asset_entry is not None:
            extra_visual_assets.append(asset_entry)
        analysis = output.get("analysis") if isinstance(output.get("analysis"), dict) else {}
        context_item = {
            "kind": "visual_reinspection",
            "bundle_id": str(output.get("bundle_id") or "").strip() or None,
            "doc_id": str(output.get("doc_id") or "").strip() or None,
            "asset_ref": str(output.get("asset_id") or "").strip() or None,
            "asset_kind": (
                output.get("asset", {}).get("kind")
                if isinstance(output.get("asset"), dict)
                else None
            ),
            "question": output.get("question"),
            "analysis_summary": _compact_text(analysis.get("summary"), limit=1000),
            "visible_text": (
                analysis.get("visible_text")[:8]
                if isinstance(analysis.get("visible_text"), list)
                else []
            ),
        }

    return (
        _merge_visual_assets(source_visual_assets, extra_visual_assets),
        _merge_document_context_items(document_context, [context_item]),
    )
def _match_visual_asset(
    source: dict[str, Any],
    visual_assets: list[dict[str, Any]],
) -> dict[str, Any] | None:
    asset_ref = str(source.get("asset_ref") or source.get("artifact_id") or "").strip()
    figure_id = str(source.get("figure_id") or "").strip()
    filename = str(source.get("filename") or "").strip().lower()
    path_value = str(source.get("path") or "").strip()
    for item in visual_assets:
        if asset_ref and asset_ref in {
            str(item.get("asset_ref") or "").strip(),
            str(item.get("artifact_id") or "").strip(),
        }:
            return item
        if figure_id and figure_id == str(item.get("figure_id") or "").strip():
            return item
        if filename and filename == str(item.get("filename") or "").strip().lower():
            return item
        if path_value and path_value == str(item.get("path") or "").strip():
            return item
    return None


def _hydrate_existing_source(
    source: dict[str, Any],
    visual_assets: list[dict[str, Any]],
) -> dict[str, Any]:
    if isinstance(source.get("image_bytes"), (bytes, bytearray)):
        return source
    matched = _match_visual_asset(source, visual_assets)
    if matched is None:
        requested = (
            str(source.get("asset_ref") or source.get("artifact_id") or source.get("filename") or "").strip()
            or "unknown asset"
        )
        raise RuntimeError(f"Slide source asset could not be resolved: {requested}")
    path = _resolve_artifact_file(str(matched.get("path") or ""))
    if path is None:
        raise RuntimeError(
            f"Slide source asset file is missing: {str(matched.get('path') or '').strip() or 'unknown path'}"
        )
    hydrated = dict(source)
    hydrated["asset_ref"] = str(matched.get("asset_ref") or "").strip() or hydrated.get("asset_ref")
    hydrated["kind"] = "from_asset"
    hydrated["image_bytes"] = path.read_bytes()
    return hydrated


def _apply_docs_parse_reverse_result(
    *,
    source_artifacts: list[dict[str, Any]],
    source_documents: list[dict[str, Any]],
    reverse_result: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    status = str(reverse_result.get("status") or "").strip().lower()
    if status and status != "completed":
        err = (
            reverse_result.get("error")
            if isinstance(reverse_result.get("error"), dict)
            else {}
        )
        code = str(err.get("code") or "DOCS_PARSE_FAILED").strip()
        message = str(err.get("message") or "Delegated docs parsing failed.").strip()
        raise RuntimeError(f"{code}: {message}")
    merged_artifacts = _merge_artifact_descriptors(
        source_artifacts,
        reverse_result.get("artifacts"),
    )
    output = reverse_result.get("output") if isinstance(reverse_result.get("output"), dict) else {}
    merged_documents = _merge_document_summaries(
        source_documents,
        output.get("documents"),
        _document_summaries_from_artifacts(merged_artifacts),
    )
    return merged_artifacts, merged_documents


def _hydrate_slot_source(
    content: dict[str, Any],
    *,
    visual_assets: list[dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(content, dict):
        return content
    source = content.get("source")
    if not isinstance(source, dict):
        return content
    kind = str(source.get("kind") or "").strip().lower()
    if kind not in _EXISTING_ASSET_SOURCE_KINDS:
        return content
    hydrated = dict(content)
    hydrated["source"] = _hydrate_existing_source(dict(source), visual_assets)
    return hydrated


def _hydrate_deck_plan_sources(
    deck_plan: dict[str, Any] | None,
    *,
    visual_assets: list[dict[str, Any]],
) -> dict[str, Any]:
    if not deck_plan:
        return {}
    updated_plan = _json_safe_copy(deck_plan)
    slides = list(updated_plan.get("slides") or [])
    for slide_def in slides:
        if not isinstance(slide_def, dict):
            continue
        for key in ("image", "content", "left_content", "right_content"):
            content = slide_def.get(key)
            if isinstance(content, dict):
                slide_def[key] = _hydrate_slot_source(
                    content,
                    visual_assets=visual_assets,
                )
        assignments = slide_def.get("assignments")
        if isinstance(assignments, dict):
            updated_assignments: dict[str, Any] = {}
            for assignment_idx, assignment in assignments.items():
                if isinstance(assignment, dict):
                    updated_assignments[str(assignment_idx)] = _hydrate_slot_source(
                        assignment,
                        visual_assets=visual_assets,
                    )
                else:
                    updated_assignments[str(assignment_idx)] = assignment
            slide_def["assignments"] = updated_assignments
    updated_plan["slides"] = slides
    return updated_plan


def _hydrate_edit_operation_sources(
    edit_operations: list[dict[str, Any]] | None,
    *,
    visual_assets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    updated_operations = _json_safe_copy(edit_operations or [])
    for operation in updated_operations:
        if not isinstance(operation, dict):
            continue
        if str(operation.get("action") or "").strip().lower() != "replace_image":
            continue
        new_image = operation.get("new_image")
        if not isinstance(new_image, dict):
            continue
        source = new_image.get("source")
        if not isinstance(source, dict):
            continue
        kind = str(source.get("kind") or "").strip().lower()
        if kind not in _EXISTING_ASSET_SOURCE_KINDS:
            continue
        hydrated_source = _hydrate_existing_source(dict(source), visual_assets)
        updated_new_image = dict(new_image)
        updated_new_image["source"] = hydrated_source
        updated_new_image["image_bytes"] = hydrated_source.get("image_bytes")
        operation["new_image"] = updated_new_image
    return updated_operations


def _bump_round(state: SlideWorkflowState, *, action: str) -> dict[str, Any]:
    rounds = int(state.get("tool_round") or 0) + 1
    max_rounds = int(state.get("max_tool_rounds") or 10)
    if rounds > max_rounds:
        return {
            "tool_round": rounds,
            "next_action": "finish",
            "agent_result": _result_error(
                code="INTERNAL_ERROR",
                retryable=False,
                message=f"Slide workflow exceeded max tool rounds while running {action}.",
            ),
        }
    return {"tool_round": rounds}


async def _step_plan_update(
    ctx: _GraphCtx, step: int, status: str, note: str | None = None
) -> None:
    step_plan = getattr(ctx.agent, "step_plan", None)
    if step_plan is None:
        return
    try:
        await step_plan.update(step, status, note=note)
    except Exception:
        logger.debug("slide.graph.step_plan_update_failed", exc_info=True)


_RESUME_STATE_KEYS = (
    "tool_round",
    "max_tool_rounds",
    "description",
    "template",
    "source_pptx_path",
    "edit_request",
    "input_data",
    "source_artifacts",
    "source_documents",
    "source_visual_assets",
    "source_document_context",
    "source_artifacts_prepared",
    "llm_deck_plan",
    "llm_edit_operations",
    "llm_action",
    "prepared_slides",
    "assets_ready",
    "pptx_path",
    "build_ok",
    "build_error",
    "built",
    "validation_results",
    "validation_pass",
    "validation_issues",
    "validation_attempts",
    "max_validation_attempts",
    "plan_active",
    "plan_step",
    "plan_total_steps",
    "plan_steps",
    "accumulated_artifacts",
    "accumulated_outputs",
    "doc_context_request_count",
    "max_doc_context_requests",
    "pending_asset_request",
    "analyzed",
    "assets_prepared",
    "validated",
    "finalized",
)


def _json_safe_copy(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _json_safe_copy(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(k): _json_safe_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe_copy(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe_copy(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _build_resume_state(state: SlideWorkflowState) -> dict[str, Any]:
    resume_state: dict[str, Any] = {}
    for key in _RESUME_STATE_KEYS:
        if key in state:
            resume_state[key] = _json_safe_copy(state[key])
    return resume_state


def _provenance_payload(task: TaskEnvelope) -> dict[str, Any]:
    request_id = None
    if isinstance(task.input, dict):
        raw_request_id = task.input.get("request_id")
        if isinstance(raw_request_id, str):
            request_id = raw_request_id.strip() or None
    return {
        "child_task_id": task.task_id,
        "session_id": task.session_id,
        "request_id": request_id,
        "channel": task.channel,
        "source": task.source,
        "source_id": task.source_id,
        "parent_task_id": task.parent_task_id,
    }


def _resolve_artifact_file(path_value: str | None) -> Path | None:
    raw = str(path_value or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = (BACKEND_ROOT / raw).resolve()
    return path if path.exists() and path.is_file() else None


def _extract_reverse_image_bytes(reverse_result: dict[str, Any]) -> bytes:
    status = str(reverse_result.get("status") or "").strip().lower()
    if status and status != "completed":
        err = (
            reverse_result.get("error")
            if isinstance(reverse_result.get("error"), dict)
            else {}
        )
        code = str(err.get("code") or "ASSET_DELEGATION_FAILED").strip()
        message = str(err.get("message") or "Delegated asset generation failed.").strip()
        raise RuntimeError(f"{code}: {message}")

    output = reverse_result.get("output") if isinstance(reverse_result.get("output"), dict) else {}
    if isinstance(output.get("image_bytes"), (bytes, bytearray)):
        return bytes(output["image_bytes"])

    for artifact in reverse_result.get("artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        mime = str(artifact.get("mime") or "").strip().lower()
        path = _resolve_artifact_file(artifact.get("path"))
        if path is None:
            continue
        if mime.startswith("image/") or path.suffix.lower() in {
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
        }:
            data = path.read_bytes()
            if data:
                return data

    raise RuntimeError(
        "Delegated asset completed but did not produce a readable image artifact."
    )


def _apply_reverse_asset(
    *,
    deck_plan: dict[str, Any] | None,
    edit_operations: list[dict[str, Any]] | None,
    pending_asset: dict[str, Any],
    reverse_result: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    deck_plan = deck_plan or {}
    edit_operations = list(edit_operations or [])

    if "operation_index" in pending_asset:
        raw_operation_index = pending_asset.get("operation_index")
        operation_index = (
            int(raw_operation_index)
            if raw_operation_index is not None
            else -1
        )
        if operation_index < 0 or operation_index >= len(edit_operations):
            raise RuntimeError(
                "Delegated asset resume could not find the pending slide.edit operation."
            )
        image_bytes = _extract_reverse_image_bytes(reverse_result)
        operation = _json_safe_copy(edit_operations[operation_index])
        new_image = operation.get("new_image")
        if not isinstance(new_image, dict):
            new_image = {}
        source = new_image.get("source")
        if not isinstance(source, dict):
            source = {}
        source["image_bytes"] = image_bytes
        source["kind"] = "generated"
        new_image["source"] = source
        new_image["image_bytes"] = image_bytes
        operation["new_image"] = new_image
        edit_operations[operation_index] = operation
        return _json_safe_copy(deck_plan), edit_operations

    image_bytes = _extract_reverse_image_bytes(reverse_result)
    slide_number = int(pending_asset.get("slide_number") or 0)
    slot = str(pending_asset.get("slot") or "").strip()
    if slide_number <= 0 or not slot:
        raise RuntimeError("Delegated asset resume is missing slide/slot targeting.")
    if not deck_plan:
        raise RuntimeError("Delegated asset resume is missing the current deck plan.")
    slides = _json_safe_copy(deck_plan.get("slides") or [])
    matched = None
    for slide in slides:
        if int(slide.get("slide_number") or 0) == slide_number:
            matched = slide
            break
    if matched is None:
        raise RuntimeError(
            f"Delegated asset resume could not find slide {slide_number} in the deck plan."
        )

    assignment_idx = str(pending_asset.get("assignment_idx") or "").strip()
    if assignment_idx:
        assignments = matched.get("assignments")
        if not isinstance(assignments, dict):
            assignments = {}
        content = assignments.get(assignment_idx)
        if not isinstance(content, dict):
            content = {}
    else:
        content = matched.get(slot)
        if not isinstance(content, dict):
            content = {}
    source = content.get("source")
    if not isinstance(source, dict):
        source = {}
    source["image_bytes"] = image_bytes
    source["kind"] = "generated"
    content["source"] = source
    if str(content.get("type") or "").strip().lower() != "image":
        content["type"] = "image"
    if assignment_idx:
        assignments[assignment_idx] = content
        matched["assignments"] = assignments
    else:
        matched[slot] = content

    updated_plan = _json_safe_copy(deck_plan)
    updated_plan["slides"] = slides
    return updated_plan, edit_operations


def _extract_bounding_boxes(slide_def: dict[str, Any]) -> list:
    """Extract bounding boxes from a slide definition for layout validation."""
    from .layout_engine import BoundingBox

    elements: list[BoundingBox] = []
    slide_num = slide_def.get("slide_number", 0)

    # Title
    if slide_def.get("title"):
        elements.append(
            BoundingBox(
                x=0.5,
                y=0.3,
                width=12.3,
                height=0.8,
                label=f"S{slide_num}:title",
                element_type="text",
            )
        )

    # Subtitle
    if slide_def.get("subtitle"):
        elements.append(
            BoundingBox(
                x=0.5,
                y=1.2,
                width=12.3,
                height=0.6,
                label=f"S{slide_num}:subtitle",
                element_type="text",
            )
        )

    # Standard content slots
    for slot_name in ("content", "left_content", "right_content"):
        slot = slide_def.get(slot_name, {})
        if not isinstance(slot, dict):
            continue
        slot_type = slot.get("type", "")

        if slot_name == "content":
            x, y, w, h = 0.8, 1.5, 11.7, 5.5
        elif slot_name == "left_content":
            x, y, w, h = 0.8, 1.5, 5.5, 5.5
        else:
            x, y, w, h = 6.8, 1.5, 5.5, 5.5

        label = f"S{slide_num}:{slot_name}"
        elem_type = "text"
        if slot_type == "chart":
            elem_type = "chart"
        elif slot_type == "code_chart":
            elem_type = "chart"
        elif slot_type == "flow_diagram":
            elem_type = "shape"
            # Count boxes in flow diagram
            boxes = slot.get("boxes", [])
            if boxes:
                direction = slot.get("direction", "horizontal")
                position = slot.get("position", {})
                box_size = slot.get("box_size", {})
                gap = slot.get("gap", 0.8)
                bw = box_size.get("width", 2.5)
                bh = box_size.get("height", 1)
                bx = position.get("x_inches", 1.5)
                by = position.get("y_inches", 2.5)
                for i, box in enumerate(boxes):
                    elements.append(
                        BoundingBox(
                            x=bx,
                            y=by,
                            width=bw,
                            height=bh,
                            label=f"S{slide_num}:flow_box_{i}",
                            element_type="shape",
                        )
                    )
                    if direction == "horizontal":
                        bx += bw + gap
                    else:
                        by += bh + gap
                continue  # Skip the default slot element

        elements.append(
            BoundingBox(
                x=x, y=y, width=w, height=h, label=label, element_type=elem_type
            )
        )

    # Image
    image_def = slide_def.get("image", {})
    if image_def:
        placement = image_def.get("placement", {})
        elements.append(
            BoundingBox(
                x=placement.get("x_inches", 6),
                y=placement.get("y_inches", 1.5),
                width=placement.get("width_inches", 6),
                height=placement.get("height_inches", 4),
                label=f"S{slide_num}:image",
                element_type="image",
            )
        )

    # Table
    table_def = slide_def.get("table", {})
    if table_def:
        rows = len(table_def.get("rows", [])) + 1  # +1 for header
        elements.append(
            BoundingBox(
                x=0.8,
                y=1.5,
                width=11.5,
                height=0.4 * rows,
                label=f"S{slide_num}:table",
                element_type="table",
            )
        )

    # Chart (native pptx chart)
    chart_def = slide_def.get("chart", {})
    if chart_def:
        elements.append(
            BoundingBox(
                x=1,
                y=1.5,
                width=11,
                height=5,
                label=f"S{slide_num}:chart",
                element_type="chart",
            )
        )

    return elements


def _build_graph(cfg: SlideAgentConfig, ctx: _GraphCtx):
    graph = StateGraph(SlideWorkflowState)
    task = ctx.task
    agent = ctx.agent

    async def prepare_source_materials(state: SlideWorkflowState) -> dict[str, Any]:
        """Normalize uploaded/source artifacts and parse raw documents when needed."""
        bump = _bump_round(state, action="prepare_source_materials")
        source_artifacts = _merge_artifact_descriptors(
            state.get("source_artifacts"),
            _collect_task_source_artifacts(task),
        )
        parsed_document_summaries = _document_summaries_from_artifacts(source_artifacts)
        source_documents = _merge_document_summaries(
            state.get("source_documents"),
            parsed_document_summaries,
        )

        if state.get("source_artifacts_prepared"):
            visual_assets = _merge_visual_assets(
                state.get("source_visual_assets"),
                _extract_visual_assets_from_artifacts(source_artifacts),
            )
            return {
                **bump,
                "source_artifacts": source_artifacts,
                "source_documents": _merge_document_summaries(
                    source_documents,
                    parsed_document_summaries,
                ),
                "source_visual_assets": visual_assets,
                "source_document_context": list(
                    state.get("source_document_context") or []
                ),
                "source_artifacts_prepared": True,
                "suspended": False,
            }

        parsed_source_artifact_ids = {
            str(item.get("artifact_id") or "").strip()
            for item in source_documents
            if str(item.get("artifact_id") or "").strip()
        }
        raw_document_artifacts = [
            artifact
            for artifact in source_artifacts
            if _is_document_artifact(artifact) and not _is_docs_parser_artifact(artifact)
            and str(artifact.get("artifact_id") or "").strip()
            not in parsed_source_artifact_ids
        ]

        if raw_document_artifacts:
            pending_asset = {
                "request_kind": "docs_parse",
                "target_intent": "docs.parse_bundle",
                "target_agent_id": cfg.docs_parser_agent_id,
                "source_artifact_ids": [
                    str(item.get("artifact_id") or "").strip()
                    for item in raw_document_artifacts
                    if str(item.get("artifact_id") or "").strip()
                ],
            }
            resume_state = _build_resume_state(
                {
                    **state,
                    "source_artifacts": source_artifacts,
                    "source_documents": source_documents,
                    "source_visual_assets": _merge_visual_assets(
                        state.get("source_visual_assets"),
                        _extract_visual_assets_from_artifacts(source_artifacts),
                    ),
                    "source_document_context": list(
                        state.get("source_document_context") or []
                    ),
                    "source_artifacts_prepared": False,
                    "pending_asset_request": pending_asset,
                    "suspended": True,
                }
            )
            result = await agent.request_orchestrator_delegate(
                current_task=task,
                target_intent="docs.parse_bundle",
                target_input={
                    "bundle_label": (
                        str(task.input.get("bundle_label") or "").strip()
                        or str(task.input.get("description") or task.input.get("query") or "").strip()[:120]
                        or f"slide_agent_{task.task_id}"
                    )
                },
                target_agent_id=cfg.docs_parser_agent_id,
                input_artifacts=raw_document_artifacts,
                resume_payload=resume_state,
                reason="slide_agent needs parsed document bundles and extracted visual assets",
            )
            reverse_task_id = str(result.get("reverse_task_id") or "").strip()
            if not reverse_task_id:
                raise RuntimeError(
                    "Slide source-material delegation did not return a reverse_task_id."
                )
            await agent.emit_event(
                task.task_id,
                "task.suspended",
                {
                    "reason": "slide_prepare_source_materials",
                    **_provenance_payload(task),
                    "reverse_task_id": reverse_task_id,
                    "target_intent": "docs.parse_bundle",
                    "target_agent_id": cfg.docs_parser_agent_id,
                    "resume_intent": "agent.resume",
                    "resume_payload": resume_state,
                    "source_artifact_count": len(raw_document_artifacts),
                },
            )
            return {
                **bump,
                "source_artifacts": source_artifacts,
                "source_documents": source_documents,
                "source_visual_assets": _merge_visual_assets(
                    state.get("source_visual_assets"),
                    _extract_visual_assets_from_artifacts(source_artifacts),
                ),
                "source_document_context": list(
                    state.get("source_document_context") or []
                ),
                "pending_asset_request": pending_asset,
                "source_artifacts_prepared": False,
                "suspended": True,
            }

        merged_documents = _merge_document_summaries(
            source_documents,
            parsed_document_summaries,
        )
        visual_assets = _merge_visual_assets(
            state.get("source_visual_assets"),
            _extract_visual_assets_from_artifacts(source_artifacts),
        )
        return {
            **bump,
            "source_artifacts": source_artifacts,
            "source_documents": merged_documents,
            "source_visual_assets": visual_assets,
            "source_document_context": list(state.get("source_document_context") or []),
            "source_artifacts_prepared": True,
            "suspended": False,
        }

    async def analyze_request(state: SlideWorkflowState) -> dict[str, Any]:
        """Analyze the user's request and produce a DeckPlan or edit operations."""
        bump = _bump_round(state, action="analyze")
        if state.get("analyzed") and (
            state.get("llm_deck_plan")
            or (state.get("intent") == "slide.edit" and "llm_edit_operations" in state)
        ):
            return {**bump, "suspended": False}
        intent = state.get("intent", "")
        source_documents = list(state.get("source_documents") or [])
        source_visual_assets = list(state.get("source_visual_assets") or [])
        source_document_context = list(state.get("source_document_context") or [])
        document_context_payload = _build_document_context_prompt_payload(
            source_document_context
        )

        async def _suspend_for_docs_context(raw_request: Any) -> dict[str, Any]:
            request_count = int(state.get("doc_context_request_count") or 0)
            max_requests = int(
                state.get("max_doc_context_requests")
                or cfg.slide_max_doc_context_requests
                or 1
            )
            if request_count >= max_requests:
                return {
                    **bump,
                    "next_action": "finish",
                    "agent_result": _result_error(
                        code="DOC_CONTEXT_REQUEST_LIMIT_EXCEEDED",
                        retryable=False,
                        message=(
                            "Slide planning asked for more document lookups than allowed "
                            "for a single task."
                        ),
                        next_action="revise_input",
                    ),
                }
            try:
                normalized_request = _normalize_doc_context_request(
                    raw_request,
                    source_documents=source_documents,
                    source_visual_assets=source_visual_assets,
                )
            except Exception as exc:
                return {
                    **bump,
                    "next_action": "finish",
                    "agent_result": _result_error(
                        code="INVALID_DOC_CONTEXT_REQUEST",
                        retryable=False,
                        message=str(exc),
                        next_action="revise_input",
                    ),
                }

            pending_request = {
                "request_kind": "docs_context",
                "target_intent": normalized_request["target_intent"],
                "target_agent_id": cfg.docs_parser_agent_id,
                **normalized_request["target_input"],
            }
            resume_state = _build_resume_state(
                {
                    **state,
                    "source_documents": source_documents,
                    "source_visual_assets": source_visual_assets,
                    "source_document_context": source_document_context,
                    "pending_asset_request": pending_request,
                    "doc_context_request_count": request_count + 1,
                    "suspended": True,
                    "analyzed": False,
                }
            )
            result = await agent.request_orchestrator_delegate(
                current_task=task,
                target_intent=normalized_request["target_intent"],
                target_input=normalized_request["target_input"],
                target_agent_id=cfg.docs_parser_agent_id,
                resume_payload=resume_state,
                reason=normalized_request["reason"],
            )
            reverse_task_id = str(result.get("reverse_task_id") or "").strip()
            if not reverse_task_id:
                raise RuntimeError(
                    "Slide doc-context delegation did not return a reverse_task_id."
                )
            await agent.emit_event(
                task.task_id,
                "task.suspended",
                {
                    "reason": "slide_delegate_doc_context",
                    **_provenance_payload(task),
                    "reverse_task_id": reverse_task_id,
                    "target_intent": normalized_request["target_intent"],
                    "target_agent_id": cfg.docs_parser_agent_id,
                    "resume_intent": "agent.resume",
                    "resume_payload": resume_state,
                    "request_kind": "docs_context",
                }
                | normalized_request["target_input"],
            )
            return {
                **bump,
                "source_documents": source_documents,
                "source_visual_assets": source_visual_assets,
                "source_document_context": source_document_context,
                "pending_asset_request": pending_request,
                "doc_context_request_count": request_count + 1,
                "suspended": True,
                "analyzed": False,
            }

        # Read learnings for user preferences
        learnings_context = ""
        learnings_path = AGENT_ROOT / "store" / "learnings.md"
        if learnings_path.exists():
            try:
                learnings_content = learnings_path.read_text(encoding="utf-8").strip()
                if learnings_content and len(learnings_content) > 30:
                    learnings_context = f"\n\n---\nUser preferences from past interactions:\n{learnings_content[:3000]}\n"
            except Exception:
                pass

        if intent == "slide.edit":
            # Parse existing deck structure
            source_path = state.get("source_pptx_path", "")
            if source_path and Path(source_path).exists():
                from .slide_builder import SlideBuilder as SB

                builder = SB(cfg.templates_dir)
                prs = builder.load_existing(Path(source_path))
                structure = builder.extract_structure(prs)
            else:
                structure = {"slide_count": 0, "slides": []}

            source_materials = _build_source_material_prompt_payload(
                documents=source_documents,
                visual_assets=source_visual_assets,
            )

            edit_result = await plan_edit(
                cfg=cfg,
                http_client=httpx.AsyncClient(timeout=30),
                existing_structure=structure,
                edit_request=state.get("edit_request", ""),
                source_materials=source_materials,
                document_context=document_context_payload
                if document_context_payload["items"]
                else None,
                task_id=task.task_id,
                session_id=task.session_id,
                source=task.source,
                source_id=task.source_id,
                channel=task.channel,
            )
            if edit_result:
                action = edit_result.get("action", "edit")
                if action == "create_plan":
                    raw_steps = edit_result.get("steps") or []
                    steps = [str(s).strip() for s in raw_steps if str(s).strip()][:8]
                    if steps:
                        step_plan = getattr(agent, "step_plan", None)
                        if step_plan is not None:
                            try:
                                await step_plan.create(steps)
                                await step_plan.update(1, "in_progress")
                            except Exception:
                                pass
                        return {
                            **bump,
                            "plan_active": True,
                            "plan_steps": steps,
                            "plan_total_steps": len(steps),
                            "plan_step": 1,
                            "analyzed": False,
                        }
                if action == "request_doc_context":
                    return await _suspend_for_docs_context(
                        edit_result.get("doc_request")
                        or edit_result.get("docs_request")
                    )

                return {
                    **bump,
                    "llm_edit_operations": edit_result.get("operations", []),
                    "llm_action": "edit",
                    "analyzed": True,
                }
        else:
            # slide.create
            desc = state.get("description", "")
            if not desc:
                desc = task.input.get("description", "") or task.input.get("query", "")

            # Inject plan step context if active
            plan_step_text = ""
            if state.get("plan_active"):
                plan_steps = state.get("plan_steps") or []
                current_step = int(state.get("plan_step") or 1)
                if 0 < current_step <= len(plan_steps):
                    plan_step_text = plan_steps[current_step - 1]
                    desc = f"{desc}\n\n[Purpose: {plan_step_text}]"

            # Extract template layout structure for template-guided planning
            from .slide_builder import SlideBuilder as _SB
            from .templates_registry import get_template_descriptions

            template_builder = _SB(cfg.templates_dir)
            template_name = state.get("template", "") or cfg.default_template
            template_prs = template_builder.load_template(template_name)
            template_layouts = template_builder.extract_layouts(template_prs)

            # Inject template structure + registry into input_data for LLM
            input_data = dict(state.get("input_data") or {})
            input_data["_template_layouts"] = template_layouts
            input_data["_available_templates"] = get_template_descriptions()
            source_materials = _build_source_material_prompt_payload(
                documents=source_documents,
                visual_assets=source_visual_assets,
            )
            if source_materials["documents"] or source_materials["visual_assets"]:
                input_data["_source_materials"] = source_materials
            if document_context_payload["items"]:
                input_data["_document_context"] = document_context_payload

            deck_plan = await plan_deck(
                cfg=cfg,
                http_client=httpx.AsyncClient(timeout=30),
                description=desc,
                template=template_name,
                input_data=input_data,
                learnings_context=learnings_context,
                task_id=task.task_id,
                session_id=task.session_id,
                source=task.source,
                source_id=task.source_id,
                channel=task.channel,
            )
            if deck_plan:
                action = deck_plan.get("action", "generate")
                if action == "create_plan":
                    raw_steps = deck_plan.get("steps") or []
                    steps = [str(s).strip() for s in raw_steps if str(s).strip()][:8]
                    if steps:
                        step_plan = getattr(agent, "step_plan", None)
                        if step_plan is not None:
                            try:
                                await step_plan.create(steps)
                                await step_plan.update(1, "in_progress")
                            except Exception:
                                pass
                        return {
                            **bump,
                            "plan_active": True,
                            "plan_steps": steps,
                            "plan_total_steps": len(steps),
                            "plan_step": 1,
                            "analyzed": False,
                        }
                if action == "request_doc_context":
                    return await _suspend_for_docs_context(
                        deck_plan.get("doc_request")
                        or deck_plan.get("docs_request")
                    )

                return {
                    **bump,
                    "llm_deck_plan": deck_plan,
                    "llm_action": "create",
                    "analyzed": True,
                }

        return {
            **bump,
            "next_action": "finish",
            "agent_result": _result_error(
                code="INTERNAL_ERROR",
                retryable=True,
                message="Internal LLM failed to analyze the slide request.",
                next_action="retry",
            ),
        }

    async def prepare_assets(state: SlideWorkflowState) -> dict[str, Any]:
        """Prepare images and diagrams for slides.

        Delegates to diagram/image agents via orchestrator reverse tasks.
        Stores delegation requests in state for the build phase to use.
        """
        bump = _bump_round(state, action="prepare_assets")
        deck_plan = state.get("llm_deck_plan", {})
        slides = _json_safe_copy(deck_plan.get("slides") or [])
        edit_operations = _json_safe_copy(state.get("llm_edit_operations") or [])
        visual_assets = list(state.get("source_visual_assets") or [])

        async def _suspend_for_generated_asset(
            *,
            source: dict[str, Any],
            pending_asset: dict[str, Any],
            reason_suffix: str,
            deck_plan_override: dict[str, Any] | None = None,
            prepared_slides_override: list[dict[str, Any]] | None = None,
            edit_operations_override: list[dict[str, Any]] | None = None,
        ) -> dict[str, Any]:
            agent_type = (
                str(source.get("agent") or "image").strip().lower() or "image"
            )
            prompt = str(source.get("prompt") or "").strip()
            if not prompt:
                raise RuntimeError("Generated slide asset source is missing a prompt.")

            if agent_type == "diagram":
                target_intent = "diagram.create"
                target_input = {
                    "description": prompt,
                    "preferred_renderer": source.get("renderer", "mermaid"),
                    "output_format": "png",
                }
                target_agent_id = cfg.diagram_agent_id
            else:
                target_intent = "image.generate"
                target_input = {
                    "prompt": prompt,
                    "size": "1024x1024",
                    "response_format": "b64_json",
                }
                target_agent_id = cfg.image_agent_id

            pending_asset = {
                **pending_asset,
                "agent_type": agent_type,
                "target_intent": target_intent,
                "target_agent_id": target_agent_id,
            }
            resume_state = _build_resume_state(
                {
                    **state,
                    "llm_deck_plan": deck_plan_override
                    if deck_plan_override is not None
                    else {**deck_plan, "slides": slides},
                    "llm_edit_operations": edit_operations_override
                    if edit_operations_override is not None
                    else edit_operations,
                    "prepared_slides": prepared_slides_override
                    if prepared_slides_override is not None
                    else slides,
                    "pending_asset_request": pending_asset,
                    "assets_ready": False,
                    "assets_prepared": False,
                    "suspended": True,
                }
            )
            result = await agent.request_orchestrator_delegate(
                current_task=task,
                target_intent=target_intent,
                target_input=target_input,
                target_agent_id=target_agent_id,
                resume_payload=resume_state,
                reason=f"slide_agent needs {agent_type} for {reason_suffix}",
            )
            reverse_task_id = str(result.get("reverse_task_id") or "").strip()
            if not reverse_task_id:
                raise RuntimeError(
                    "Slide asset delegation did not return a reverse_task_id."
                )
            await agent.emit_event(
                task.task_id,
                "task.suspended",
                {
                    "reason": "slide_delegate_asset",
                    **_provenance_payload(task),
                    "reverse_task_id": reverse_task_id,
                    "target_intent": target_intent,
                    "target_agent_id": target_agent_id,
                    "resume_intent": "agent.resume",
                    "resume_payload": resume_state,
                    **pending_asset,
                },
            )
            return {
                **bump,
                "llm_deck_plan": deck_plan_override
                if deck_plan_override is not None
                else {**deck_plan, "slides": slides},
                "llm_edit_operations": edit_operations_override
                if edit_operations_override is not None
                else edit_operations,
                "prepared_slides": prepared_slides_override
                if prepared_slides_override is not None
                else slides,
                "pending_asset_request": pending_asset,
                "suspended": True,
                "assets_ready": False,
                "assets_prepared": False,
            }

        if state.get("intent") == "slide.edit":
            try:
                edit_operations = _hydrate_edit_operation_sources(
                    edit_operations,
                    visual_assets=visual_assets,
                )
            except Exception as exc:
                return {
                    **bump,
                    "next_action": "finish",
                    "agent_result": _result_error(
                        code="SOURCE_ASSET_RESOLUTION_FAILED",
                        retryable=False,
                        message=str(exc),
                        next_action="revise_input",
                    ),
                }
            for operation_index, operation in enumerate(edit_operations):
                if not isinstance(operation, dict):
                    continue
                if str(operation.get("action") or "").strip().lower() != "replace_image":
                    continue
                new_image = operation.get("new_image")
                if not isinstance(new_image, dict):
                    continue
                source = new_image.get("source")
                if not isinstance(source, dict) or source.get("kind") != "generate":
                    continue
                try:
                    return await _suspend_for_generated_asset(
                        source=source,
                        pending_asset={
                            "operation_index": operation_index,
                            "slide_number": operation.get("slide_number"),
                            "shape_name": operation.get("shape_name"),
                        },
                        reason_suffix=(
                            f"slide.edit operation {operation_index} on "
                            f"slide {operation.get('slide_number')}"
                        ),
                        prepared_slides_override=list(state.get("prepared_slides") or []),
                        edit_operations_override=edit_operations,
                    )
                except Exception as exc:
                    logger.warning(
                        "Delegation for slide.edit operation %s failed: %s",
                        operation_index,
                        exc,
                    )
                    return {
                        **bump,
                        "next_action": "finish",
                        "agent_result": _result_error(
                            code="ASSET_DELEGATION_FAILED",
                            retryable=False,
                            message=str(exc),
                            next_action="escalate",
                        ),
                    }
            return {
                **bump,
                "llm_edit_operations": edit_operations,
                "assets_ready": True,
                "assets_prepared": True,
                "suspended": False,
            }

        try:
            hydrated_plan = _hydrate_deck_plan_sources(
                {**deck_plan, "slides": slides},
                visual_assets=visual_assets,
            )
            slides = list(hydrated_plan.get("slides") or slides)
            deck_plan = hydrated_plan
        except Exception as exc:
            return {
                **bump,
                "next_action": "finish",
                "agent_result": _result_error(
                    code="SOURCE_ASSET_RESOLUTION_FAILED",
                    retryable=False,
                    message=str(exc),
                    next_action="revise_input",
                ),
            }

        for slide_def in slides:
            for key in ("image", "content", "left_content", "right_content"):
                content = slide_def.get(key, {})
                if not isinstance(content, dict):
                    continue
                source = content.get("source", {})
                if source.get("kind") != "generate":
                    continue

                agent_type = source.get("agent", "image")
                prompt = source.get("prompt", "")

                if not prompt:
                    continue

                # Delegate to sibling agent via orchestrator
                if agent_type == "diagram":
                    target_intent = "diagram.create"
                    target_input = {
                        "description": prompt,
                        "preferred_renderer": source.get("renderer", "mermaid"),
                        "output_format": "png",
                    }
                else:
                    target_intent = "image.generate"
                    target_input = {
                        "prompt": prompt,
                        "size": "1024x1024",
                        "response_format": "b64_json",
                    }
                target_agent_id = (
                    cfg.diagram_agent_id if agent_type == "diagram" else cfg.image_agent_id
                )

                try:
                    pending_asset = {
                        "slide_number": slide_def.get("slide_number"),
                        "slot": key,
                        "agent_type": agent_type,
                        "target_intent": target_intent,
                        "target_agent_id": target_agent_id,
                    }
                    resume_state = _build_resume_state(
                        {
                            **state,
                            "llm_deck_plan": {**deck_plan, "slides": slides},
                            "prepared_slides": slides,
                            "pending_asset_request": pending_asset,
                            "assets_ready": False,
                            "assets_prepared": False,
                            "suspended": True,
                        }
                    )
                    result = await agent.request_orchestrator_delegate(
                        current_task=task,
                        target_intent=target_intent,
                        target_input=target_input,
                        target_agent_id=target_agent_id,
                        resume_payload=resume_state,
                        reason=f"slide_agent needs {agent_type} for slide {slide_def.get('slide_number')}",
                    )
                    reverse_task_id = str(result.get("reverse_task_id") or "").strip()
                    if not reverse_task_id:
                        raise RuntimeError(
                            "Slide asset delegation did not return a reverse_task_id."
                        )
                    await agent.emit_event(
                        task.task_id,
                        "task.suspended",
                        {
                            "reason": "slide_delegate_asset",
                            **_provenance_payload(task),
                            "reverse_task_id": reverse_task_id,
                            "target_intent": target_intent,
                            "target_agent_id": target_agent_id,
                            "resume_intent": "agent.resume",
                            "resume_payload": resume_state,
                            "slide_number": slide_def.get("slide_number"),
                            "slot": key,
                        },
                    )
                    return {
                        **bump,
                        "llm_deck_plan": {**deck_plan, "slides": slides},
                        "prepared_slides": slides,
                        "pending_asset_request": pending_asset,
                        "suspended": True,
                        "assets_ready": False,
                        "assets_prepared": False,
                    }
                except Exception as exc:
                    logger.warning("Delegation to %s failed: %s", agent_type, exc)
                    return {
                        **bump,
                        "next_action": "finish",
                        "agent_result": _result_error(
                            code="ASSET_DELEGATION_FAILED",
                            retryable=False,
                            message=str(exc),
                            next_action="escalate",
                        ),
                    }

            assignments = slide_def.get("assignments")
            if isinstance(assignments, dict):
                for assignment_idx, assignment in assignments.items():
                    if not isinstance(assignment, dict):
                        continue
                    source = assignment.get("source", {})
                    if not isinstance(source, dict) or source.get("kind") != "generate":
                        continue
                    prompt = str(source.get("prompt") or "").strip()
                    if not prompt:
                        continue
                    agent_type = str(source.get("agent") or "image").strip().lower() or "image"
                    if agent_type == "diagram":
                        target_intent = "diagram.create"
                        target_input = {
                            "description": prompt,
                            "preferred_renderer": source.get("renderer", "mermaid"),
                            "output_format": "png",
                        }
                    else:
                        target_intent = "image.generate"
                        target_input = {
                            "prompt": prompt,
                            "size": "1024x1024",
                            "response_format": "b64_json",
                        }
                    target_agent_id = (
                        cfg.diagram_agent_id
                        if agent_type == "diagram"
                        else cfg.image_agent_id
                    )
                    try:
                        pending_asset = {
                            "slide_number": slide_def.get("slide_number"),
                            "slot": "assignments",
                            "assignment_idx": str(assignment_idx),
                            "agent_type": agent_type,
                            "target_intent": target_intent,
                            "target_agent_id": target_agent_id,
                        }
                        resume_state = _build_resume_state(
                            {
                                **state,
                                "llm_deck_plan": {**deck_plan, "slides": slides},
                                "prepared_slides": slides,
                                "pending_asset_request": pending_asset,
                                "assets_ready": False,
                                "assets_prepared": False,
                                "suspended": True,
                            }
                        )
                        result = await agent.request_orchestrator_delegate(
                            current_task=task,
                            target_intent=target_intent,
                            target_input=target_input,
                            target_agent_id=target_agent_id,
                            resume_payload=resume_state,
                            reason=f"slide_agent needs {agent_type} for slide {slide_def.get('slide_number')} assignment {assignment_idx}",
                        )
                        reverse_task_id = str(result.get("reverse_task_id") or "").strip()
                        if not reverse_task_id:
                            raise RuntimeError(
                                "Slide assignment asset delegation did not return a reverse_task_id."
                            )
                        await agent.emit_event(
                            task.task_id,
                            "task.suspended",
                            {
                                "reason": "slide_delegate_asset",
                                **_provenance_payload(task),
                                "reverse_task_id": reverse_task_id,
                                "target_intent": target_intent,
                                "target_agent_id": target_agent_id,
                                "resume_intent": "agent.resume",
                                "resume_payload": resume_state,
                                "slide_number": slide_def.get("slide_number"),
                                "slot": "assignments",
                                "assignment_idx": str(assignment_idx),
                            },
                        )
                        return {
                            **bump,
                            "llm_deck_plan": {**deck_plan, "slides": slides},
                            "prepared_slides": slides,
                            "pending_asset_request": pending_asset,
                            "suspended": True,
                            "assets_ready": False,
                            "assets_prepared": False,
                        }
                    except Exception as exc:
                        logger.warning(
                            "Delegation to %s for assignment %s failed: %s",
                            agent_type,
                            assignment_idx,
                            exc,
                        )
                        return {
                            **bump,
                            "next_action": "finish",
                            "agent_result": _result_error(
                                code="ASSET_DELEGATION_FAILED",
                                retryable=False,
                                message=str(exc),
                                next_action="escalate",
                            ),
                        }

        return {
            **bump,
            "prepared_slides": slides,
            "llm_deck_plan": {**deck_plan, "slides": slides},
            "assets_ready": True,
            "assets_prepared": True,
            "suspended": False,
        }

    async def build_slides(state: SlideWorkflowState) -> dict[str, Any]:
        """Build the PPTX from the plan using python-pptx."""
        bump = _bump_round(state, action="build")

        intent = state.get("intent", "")
        task_artifacts_dir = agent._task_artifact_dir(task.task_id)
        task_artifacts_dir.mkdir(parents=True, exist_ok=True)
        output_path = task_artifacts_dir / "presentation.pptx"

        try:
            builder = SlideBuilder(cfg.templates_dir)

            if intent == "slide.edit" and state.get("source_pptx_path"):
                # Edit existing deck
                source_path = Path(state["source_pptx_path"])
                prs = builder.load_existing(source_path)
                operations = state.get("llm_edit_operations", [])
                prs = builder.apply_edits(prs, operations)
                prs.save(str(output_path))
            else:
                # Create new deck — pre-process code charts via sandbox
                deck_plan = dict(state.get("llm_deck_plan") or {})
                slides = list(
                    state.get("prepared_slides") or deck_plan.get("slides") or []
                )
                if not slides:
                    return {
                        **bump,
                        "build_ok": False,
                        "build_error": "No slides in deck plan.",
                        "built": True,
                    }

                # Process code_chart slides through sandbox
                sandbox_dir = task_artifacts_dir / "charts"
                sandbox_dir.mkdir(parents=True, exist_ok=True)

                for slide_def in slides:
                    for slot in ("content", "left_content", "right_content", "image"):
                        slot_content = slide_def.get(slot, {})
                        if not isinstance(slot_content, dict):
                            continue
                        if slot_content.get(
                            "type"
                        ) == "code_chart" and slot_content.get("code"):
                            from .code_sandbox import generate_chart

                            chart_dir = (
                                sandbox_dir
                                / f"slide_{slide_def.get('slide_number', 0)}_{slot}"
                            )
                            chart_dir.mkdir(parents=True, exist_ok=True)
                            result = generate_chart(
                                chart_code=slot_content["code"],
                                data=slot_content.get("data"),
                                output_dir=chart_dir,
                                width_inches=slot_content.get("width_inches", 10),
                                height_inches=slot_content.get("height_inches", 5.625),
                                dpi=150,
                                packages=slot_content.get("packages"),
                            )
                            if result.get("chart_bytes"):
                                slot_content["chart_bytes"] = result["chart_bytes"]
                                slot_content["type"] = "code_chart"
                            else:
                                logger.warning(
                                    "Sandbox chart failed: %s",
                                    result.get("stderr", "")[:200],
                                )

                deck_plan["slides"] = slides

                # ── Pre-build layout validation ────────────────────────
                from .layout_engine import (
                    BoundingBox,
                    LayoutEngine,
                    SlideBounds,
                )

                deck_def = deck_plan.get("deck", {})
                layout_engine = LayoutEngine(
                    SlideBounds(
                        width=deck_def.get("dimensions", {}).get("width", 13.333),
                        height=deck_def.get("dimensions", {}).get("height", 7.5),
                    )
                )

                layout_issues: list[str] = []
                for slide_def in slides:
                    elements = _extract_bounding_boxes(slide_def)
                    report = layout_engine.validate(
                        elements,
                        title_present=bool(slide_def.get("title")),
                    )
                    if not report.valid:
                        slide_num = slide_def.get("slide_number", "?")
                        for issue in report.errors:
                            layout_issues.append(
                                f"Slide {slide_num}: {issue.code}: {issue.message}"
                            )

                if layout_issues:
                    logger.warning(
                        "Pre-build layout issues found: %s",
                        "; ".join(layout_issues[:5]),
                    )

                builder.build_deck(deck_plan, output_path)

            return {
                **bump,
                "pptx_path": str(output_path),
                "build_ok": True,
                "built": True,
            }
        except Exception as exc:
            return {
                **bump,
                "build_ok": False,
                "build_error": str(exc),
                "built": True,
            }

    async def render_and_validate(state: SlideWorkflowState) -> dict[str, Any]:
        """Render slides to PNG and validate each with vision."""
        bump = _bump_round(state, action="validate")

        if not state.get("build_ok") or not state.get("pptx_path"):
            return {
                **bump,
                "validated": True,
                "validation_pass": False,
                "validation_issues": ["Build failed — nothing to validate"],
                "validation_attempts": int(state.get("validation_attempts") or 0) + 1,
            }

        attempts = int(state.get("validation_attempts") or 0) + 1
        max_attempts = int(state.get("max_validation_attempts") or 2)

        pptx_path = Path(state["pptx_path"])
        pngs = render_slides_to_png(
            pptx_path,
            libreoffice_path=cfg.libreoffice_path,
            pdftoppm_path=cfg.pdftoppm_path,
            dpi=cfg.render_dpi,
        )

        if not pngs:
            # Can't render — pass by default
            return {
                **bump,
                "validated": True,
                "validation_pass": True,
                "validation_attempts": attempts,
            }

        # Validate each slide
        all_issues: list[dict[str, Any]] = []
        deck_plan = state.get("llm_deck_plan", {})
        slides_def = deck_plan.get("slides", [])

        for png_path in pngs:
            # Extract slide number from filename (slide-1.png → 1)
            try:
                slide_num = int(png_path.stem.split("-")[1])
            except (IndexError, ValueError):
                slide_num = 1

            png_bytes = png_path.read_bytes()
            if not png_bytes or len(png_bytes) < 100:
                continue

            # Resize for vision validation (smaller = faster, fewer tokens)
            from .asset_manager import resize_image as _resize_img

            try:
                png_bytes = _resize_img(
                    png_bytes, target_width_px=960, target_height_px=540
                )
            except Exception:
                pass  # Use original if resize fails

            slide_plan = (
                slides_def[slide_num - 1] if slide_num <= len(slides_def) else None
            )

            validation = await validate_slide(
                cfg=cfg,
                http_client=httpx.AsyncClient(timeout=30),
                slide_number=slide_num,
                png_bytes=png_bytes,
                slide_plan=slide_plan,
                task_id=task.task_id,
                session_id=task.session_id,
                source=task.source,
                source_id=task.source_id,
                channel=task.channel,
            )

            if validation and not validation.get("pass", True):
                all_issues.append(
                    {
                        "slide_number": slide_num,
                        "issues": validation.get("issues", []),
                        "suggestion": validation.get("suggestion", ""),
                    }
                )

        if not all_issues:
            return {
                **bump,
                "validated": True,
                "validation_pass": True,
                "validation_results": [],
                "validation_attempts": attempts,
            }

        # Validation failed
        all_issue_texts = []
        for issue in all_issues:
            for text in issue.get("issues", []):
                all_issue_texts.append(f"Slide {issue['slide_number']}: {text}")

        if attempts >= max_attempts:
            return {
                **bump,
                "validated": True,
                "validation_pass": False,
                "validation_issues": all_issue_texts,
                "validation_results": all_issues,
                "validation_attempts": attempts,
            }

        # Repair: send feedback to LLM for corrected definitions
        repair_result = await repair_deck(
            cfg=cfg,
            http_client=httpx.AsyncClient(timeout=30),
            slide_plans=slides_def,
            validation_results=all_issues,
            task_id=task.task_id,
            session_id=task.session_id,
            source=task.source,
            source_id=task.source_id,
            channel=task.channel,
        )

        if repair_result and repair_result.get("slides"):
            # Merge repaired slides back into the plan
            repaired_slides = repair_result["slides"]
            current_slides = list(deck_plan.get("slides", []))
            for repaired in repaired_slides:
                slide_num = repaired.get("slide_number", 0)
                if 0 < slide_num <= len(current_slides):
                    current_slides[slide_num - 1] = repaired
            deck_plan["slides"] = current_slides

        return {
            **bump,
            "validated": False,
            "validation_pass": False,
            "validation_issues": all_issue_texts,
            "validation_results": all_issues,
            "validation_attempts": attempts,
            "llm_deck_plan": deck_plan,
            "built": False,  # Trigger rebuild
        }

    async def finalize(state: SlideWorkflowState) -> dict[str, Any]:
        """Build artifacts for current cycle. If plan has more steps, loop back."""
        if state.get("agent_result") is not None:
            if state.get("plan_active"):
                current = int(state.get("plan_step") or 1)
                total = int(state.get("plan_total_steps") or 0)
                for i in range(current, total + 1):
                    await _step_plan_update(
                        ctx, i, "skipped", note="Task ended before step executed"
                    )
            return {}

        pptx_path_str = state.get("pptx_path", "")
        build_ok = state.get("build_ok", False)

        task_artifacts_dir = agent._task_artifact_dir(task.task_id)
        step_artifacts: list[ArtifactManifest] = []
        step_output: dict[str, Any] = {}

        if build_ok and pptx_path_str:
            pptx_path = Path(pptx_path_str)

            # PPTX artifact
            if pptx_path.exists():
                step_artifacts.append(
                    agent._artifact_manifest(
                        task_id=task.task_id,
                        path=pptx_path,
                        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                        kind="output",
                        audience="deliverable",
                    )
                )

            # PDF export
            pdf_path = None
            if cfg.export_pdf:
                pdf_path = export_to_pdf(
                    pptx_path,
                    libreoffice_path=cfg.libreoffice_path,
                )
                if pdf_path and pdf_path.exists():
                    step_artifacts.append(
                        agent._artifact_manifest(
                            task_id=task.task_id,
                            path=pdf_path,
                            mime="application/pdf",
                            kind="output",
                            audience="deliverable",
                        )
                    )

            # Slide preview PNGs
            pngs = render_slides_to_png(
                pptx_path,
                libreoffice_path=cfg.libreoffice_path,
                pdftoppm_path=cfg.pdftoppm_path,
                dpi=150,
                output_dir=task_artifacts_dir / "previews",
            )
            for png in pngs:
                if png.exists():
                    step_artifacts.append(
                        agent._artifact_manifest(
                            task_id=task.task_id,
                            path=png,
                            mime="image/png",
                            kind="output",
                            audience="supporting",
                        )
                    )

            deck_plan = state.get("llm_deck_plan", {})
            step_output = {
                "pptx_path": str(pptx_path),
                "pdf_path": str(pdf_path) if pdf_path else None,
                "slide_count": len(deck_plan.get("slides", [])),
                "template": deck_plan.get("deck", {}).get("template", ""),
                "title": deck_plan.get("deck", {}).get("title", ""),
                "validation_pass": state.get("validation_pass", True),
                "validation_issues": state.get("validation_issues", []),
            }
        elif state.get("llm_edit_operations"):
            # Edit path — PPTX was built with edits applied
            step_output = {
                "pptx_path": pptx_path_str,
                "operations_applied": len(state.get("llm_edit_operations", [])),
                "validation_pass": state.get("validation_pass", True),
            }

        # ── Multi-step plan: check if more steps remain ───────────────
        plan_active = state.get("plan_active", False)
        if plan_active:
            current_step = int(state.get("plan_step") or 1)
            total_steps = int(state.get("plan_total_steps") or 0)

            await _step_plan_update(
                ctx, current_step, "completed", note=step_output.get("title", "")
            )

            if current_step < total_steps:
                accumulated = list(state.get("accumulated_artifacts") or [])
                accumulated.extend(step_artifacts)
                accumulated_outputs = list(state.get("accumulated_outputs") or [])
                accumulated_outputs.append(step_output)

                return {
                    "plan_step": current_step + 1,
                    "analyzed": False,
                    "assets_prepared": False,
                    "built": False,
                    "validated": False,
                    "validation_pass": False,
                    "validation_issues": [],
                    "validation_results": [],
                    "validation_attempts": 0,
                    "llm_deck_plan": {},
                    "llm_edit_operations": [],
                    "prepared_slides": [],
                    "pptx_path": "",
                    "build_ok": False,
                    "accumulated_artifacts": accumulated,
                    "accumulated_outputs": accumulated_outputs,
                }

            all_artifacts = list(state.get("accumulated_artifacts") or [])
            all_artifacts.extend(step_artifacts)
            all_outputs = list(state.get("accumulated_outputs") or [])
            all_outputs.append(step_output)
        else:
            all_artifacts = step_artifacts
            all_outputs = [step_output] if step_output else []

        return {
            "agent_result": AgentResult(
                status="completed",
                output={
                    "decks": all_outputs,
                    "count": len(all_outputs),
                },
                artifacts=all_artifacts,
                error=None,
            ),
            "finalized": True,
        }

    # ── Graph topology (inspired by diagram_graph.py) ─────────────────

    def route_after_analyze(state: SlideWorkflowState) -> str:
        if state.get("agent_result") is not None:
            return "finish"
        if state.get("plan_active") and not state.get("analyzed"):
            return "analyze"
        if not state.get("analyzed"):
            return "finish"
        return "assets"

    def route_after_source_prep(state: SlideWorkflowState) -> str:
        if state.get("agent_result") is not None:
            return "finish"
        if state.get("suspended"):
            return "finish"
        if not state.get("source_artifacts_prepared"):
            return "finish"
        return "analyze"

    def route_after_assets(state: SlideWorkflowState) -> str:
        if state.get("agent_result") is not None:
            return "finish"
        if state.get("suspended"):
            return "finish"
        return "build"

    def route_after_build(state: SlideWorkflowState) -> str:
        if state.get("agent_result") is not None:
            return "finish"
        if not state.get("build_ok"):
            return "finish"
        return "validate"

    def route_after_validate(state: SlideWorkflowState) -> str:
        if state.get("validation_pass") or state.get("validated"):
            return "finalize"
        return "build"  # Rebuild after repair

    def route_after_finalize(state: SlideWorkflowState) -> str:
        if state.get("plan_active") and not state.get("finalized"):
            return "analyze"
        return "end"

    # Nodes
    graph.add_node("prepare_source_materials", prepare_source_materials)
    graph.add_node("analyze_request", analyze_request)
    graph.add_node("prepare_assets", prepare_assets)
    graph.add_node("build_slides", build_slides)
    graph.add_node("render_and_validate", render_and_validate)
    graph.add_node("finalize", finalize)

    # Edges
    graph.add_edge(START, "prepare_source_materials")
    graph.add_conditional_edges(
        "prepare_source_materials",
        route_after_source_prep,
        {"analyze": "analyze_request", "finish": END},
    )
    graph.add_conditional_edges(
        "analyze_request",
        route_after_analyze,
        {"assets": "prepare_assets", "analyze": "analyze_request", "finish": END},
    )
    graph.add_conditional_edges(
        "prepare_assets",
        route_after_assets,
        {"build": "build_slides", "finish": END},
    )
    graph.add_conditional_edges(
        "build_slides",
        route_after_build,
        {"validate": "render_and_validate", "finish": END},
    )
    graph.add_conditional_edges(
        "render_and_validate",
        route_after_validate,
        {"finalize": "finalize", "build": "build_slides"},
    )
    graph.add_conditional_edges(
        "finalize",
        route_after_finalize,
        {"analyze": "analyze_request", "end": END},
    )

    return graph.compile()


async def run_slide_langgraph(
    *, agent: Any, task: TaskEnvelope
) -> AgentResult | TaskInProgress:
    """Run a bounded LangGraph workflow for slide specialist tasks."""
    cfg: SlideAgentConfig = agent._cfg
    resume_block = task.input.get("_resume") if isinstance(task.input.get("_resume"), dict) else {}
    resume_state = (
        resume_block.get("resume_state")
        if isinstance(resume_block.get("resume_state"), dict)
        else {}
    )
    reverse_result = (
        resume_block.get("reverse_result")
        if isinstance(resume_block.get("reverse_result"), dict)
        else {}
    )
    reverse_task = (
        resume_block.get("reverse_task")
        if isinstance(resume_block.get("reverse_task"), dict)
        else {}
    )

    desc = task.input.get("description", "") or task.input.get("query", "")
    template = task.input.get("template", cfg.default_template)

    if not desc and task.intent == "slide.create":
        return AgentResult(
            status="failed",
            output={},
            artifacts=[],
            error=AgentError(
                code="INVALID_INPUT",
                retryable=False,
                message="slide.create requires a 'description' field.",
                next_action="escalate",
            ),
        )

    app = _build_graph(cfg, ctx=_GraphCtx(agent=agent, task=task))
    initial_state: SlideWorkflowState = {
        "intent": task.intent,
        "tool_round": 0,
        "max_tool_rounds": cfg.slide_max_tool_rounds,
        "next_action": "finish",
        "agent_result": None,
        "description": desc,
        "template": template,
        "source_pptx_path": task.input.get("source_pptx_path", ""),
        "edit_request": task.input.get("edit_request", ""),
        "input_data": task.input.get("data"),
        "source_artifacts": _collect_task_source_artifacts(task),
        "source_documents": [],
        "source_visual_assets": [],
        "source_document_context": [],
        "source_artifacts_prepared": False,
        # Validation
        "validated": False,
        "validation_pass": False,
        "validation_issues": [],
        "validation_results": [],
        "validation_attempts": 0,
        "max_validation_attempts": cfg.max_validation_attempts,
        # Plan
        "plan_active": False,
        "plan_step": None,
        "plan_total_steps": 0,
        "plan_steps": [],
        "accumulated_artifacts": [],
        "accumulated_outputs": [],
        "doc_context_request_count": 0,
        "max_doc_context_requests": cfg.slide_max_doc_context_requests,
        "pending_asset_request": {},
        "suspended": False,
        "resumed": bool(resume_block),
    }
    if resume_state:
        for key in _RESUME_STATE_KEYS:
            if key in resume_state:
                initial_state[key] = resume_state[key]
    if resume_block:
        await agent.emit_event(
            task.task_id,
            "task.resumed",
            {
                **_provenance_payload(task),
                "resumed_task_id": task.task_id,
                "reverse_task_id": (
                    str(reverse_task.get("reverse_task_id") or "").strip() or None
                ),
                "target_intent": str(reverse_task.get("target_intent") or "").strip()
                or None,
                "target_agent_id": str(reverse_task.get("target_agent_id") or "").strip()
                or None,
            },
        )
    if reverse_result and initial_state.get("pending_asset_request"):
        pending_request = dict(initial_state.get("pending_asset_request") or {})
        request_kind = str(pending_request.get("request_kind") or "").strip().lower()
        if request_kind == "docs_parse":
            try:
                merged_artifacts, merged_documents = _apply_docs_parse_reverse_result(
                    source_artifacts=list(initial_state.get("source_artifacts") or []),
                    source_documents=list(initial_state.get("source_documents") or []),
                    reverse_result=reverse_result,
                )
            except Exception as exc:
                return AgentResult(
                    status="failed",
                    output={},
                    artifacts=[],
                    error=AgentError(
                        code="DOCS_PARSE_RESUME_FAILED",
                        retryable=False,
                        message=str(exc),
                        next_action="escalate",
                    ),
                )
            initial_state["source_artifacts"] = merged_artifacts
            initial_state["source_documents"] = merged_documents
            initial_state["source_visual_assets"] = _merge_visual_assets(
                initial_state.get("source_visual_assets"),
                _extract_visual_assets_from_artifacts(merged_artifacts),
            )
            initial_state["source_artifacts_prepared"] = False
            initial_state["pending_asset_request"] = {}
            initial_state["suspended"] = False
        elif request_kind == "docs_context":
            try:
                merged_visual_assets, merged_document_context = (
                    _apply_docs_context_reverse_result(
                        source_visual_assets=list(
                            initial_state.get("source_visual_assets") or []
                        ),
                        document_context=list(
                            initial_state.get("source_document_context") or []
                        ),
                        pending_request=pending_request,
                        reverse_result=reverse_result,
                    )
                )
            except Exception as exc:
                return AgentResult(
                    status="failed",
                    output={},
                    artifacts=[],
                    error=AgentError(
                        code="DOCS_CONTEXT_RESUME_FAILED",
                        retryable=False,
                        message=str(exc),
                        next_action="escalate",
                    ),
                )
            initial_state["source_visual_assets"] = merged_visual_assets
            initial_state["source_document_context"] = merged_document_context
            initial_state["pending_asset_request"] = {}
            initial_state["suspended"] = False
            initial_state["analyzed"] = False
            initial_state["assets_prepared"] = False
        else:
            try:
                updated_plan, updated_operations = _apply_reverse_asset(
                    deck_plan=dict(initial_state.get("llm_deck_plan") or {}),
                    edit_operations=list(initial_state.get("llm_edit_operations") or []),
                    pending_asset=pending_request,
                    reverse_result=reverse_result,
                )
            except Exception as exc:
                return AgentResult(
                    status="failed",
                    output={},
                    artifacts=[],
                    error=AgentError(
                        code="ASSET_RESUME_FAILED",
                        retryable=False,
                        message=str(exc),
                        next_action="escalate",
                    ),
                )
            initial_state["llm_deck_plan"] = updated_plan
            initial_state["llm_edit_operations"] = updated_operations
            initial_state["prepared_slides"] = list(
                updated_plan.get("slides")
                or initial_state.get("prepared_slides")
                or []
            )
            initial_state["pending_asset_request"] = {}
            initial_state["analyzed"] = True
            initial_state["assets_prepared"] = False
            initial_state["suspended"] = False

    final_state = await app.ainvoke(initial_state)
    if bool(final_state.get("suspended")):
        return TaskInProgress(
            task_id=task.task_id,
            idempotency_key=task.idempotency_key,
            executing_since=datetime.now(timezone.utc),
            check_after_sec=10,
        )
    result = final_state.get("agent_result")
    if isinstance(result, AgentResult):
        return result

    return AgentResult(
        status="failed",
        output={},
        artifacts=[],
        error=AgentError(
            code="INTERNAL_ERROR",
            retryable=False,
            message="Slide workflow completed without producing a result.",
            next_action="escalate",
        ),
    )
