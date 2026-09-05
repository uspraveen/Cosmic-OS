from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shared import (
    SUPPORTED_DOCUMENT_EXTENSIONS,
    SUPPORTED_DOCUMENT_MIME_TYPES,
    is_openai_gpt5_chat_model,
    normalized_reasoning_effort,
)

SUPPORTED_MIME_TYPES = SUPPORTED_DOCUMENT_MIME_TYPES
SUPPORTED_EXTENSIONS = SUPPORTED_DOCUMENT_EXTENSIONS
PAGE_BREAK_PLACEHOLDER = "\n\n[[DOCLING_PAGE_BREAK]]\n\n"


def _openai_vlm_params(*, model: str, max_new_tokens: int, reasoning_effort: str | None) -> dict[str, Any]:
    """Body params for the OpenAI-compatible VLM endpoint.

    GPT-5-family chat models reject temperature and max_tokens; they take
    max_completion_tokens plus an optional reasoning_effort instead.
    """
    params: dict[str, Any] = {"model": model}
    if is_openai_gpt5_chat_model(model):
        params["max_completion_tokens"] = max_new_tokens
        effort = normalized_reasoning_effort(model, reasoning_effort)
        if effort:
            params["reasoning_effort"] = effort
    else:
        params["max_tokens"] = max_new_tokens
        params["temperature"] = 0
    return params


@dataclass(slots=True)
class ParseRequest:
    enable_ocr: bool
    generate_page_images: bool
    generate_picture_images: bool
    picture_description: "PictureDescriptionRequest | None"
    full_page_vlm_mode: str
    use_full_page_vlm: bool
    full_page_vlm: "FullPageVlmRequest | None"
    max_file_size_bytes: int
    max_num_pages: int
    max_chunk_chars: int
    chunk_overlap_chars: int


@dataclass(slots=True)
class PictureDescriptionRequest:
    api_key: str
    api_url: str
    model: str
    preset: str
    prompt: str
    timeout_sec: float
    concurrency: int
    batch_size: int
    max_new_tokens: int
    scale: float
    picture_area_threshold: float
    classification_min_confidence: float
    classification_deny: tuple[str, ...]
    reasoning_effort: str = "xhigh"


@dataclass(slots=True)
class FullPageVlmRequest:
    api_key: str
    api_url: str
    model: str
    preset: str
    timeout_sec: float
    concurrency: int
    batch_size: int
    max_new_tokens: int
    scale: float
    reasoning_effort: str = "xhigh"


@dataclass(slots=True)
class ParsedDocument:
    title: str | None
    markdown: str
    document_json: dict[str, Any]
    chunk_index: dict[str, Any]
    page_count: int | None
    slide_count: int | None
    table_count: int
    figure_count: int
    section_count: int
    asset_files: list[tuple[str, bytes, str]]


class DoclingAdapter:
    def parse_file(
        self,
        *,
        file_path: Path,
        artifact_id: str,
        mime_type: str,
        request: ParseRequest,
        source_filename: str | None = None,
    ) -> ParsedDocument:
        document, result = self._convert(file_path=file_path, request=request)
        page_label = self._page_label(mime_type=mime_type, file_path=file_path)
        page_count = self._extract_optional_int(result, "page_count")
        slide_count = self._extract_optional_int(result, "slide_count")
        fallback_markdown = self._export_markdown(document)

        asset_files, asset_entries, figure_entries, table_entries, page_image_by_number = self._extract_assets(
            document=document,
            request=request,
        )
        page_segments = self._build_page_segments(
            document=document,
            fallback_markdown=fallback_markdown,
            fallback_count=slide_count if page_label == "SLIDE" else page_count,
        )
        markdown, page_entries, figure_entries, table_entries = self._compose_document_markdown(
            fallback_markdown=fallback_markdown,
            page_segments=page_segments,
            page_label=page_label,
            figure_entries=figure_entries,
            table_entries=table_entries,
            page_image_by_number=page_image_by_number,
        )
        slide_entries = []
        if page_label == "SLIDE":
            slide_entries = [
                {
                    "slide_id": entry["page_id"].replace("page_", "slide_", 1),
                    "slide_number": entry["page_number"],
                    "start_char": entry["start_char"],
                    "end_char": entry["end_char"],
                    "anchor_id": entry["page_id"].replace("page_", "slide_", 1),
                }
                for entry in page_entries
            ]

        chunk_index = self._build_chunk_index(
            artifact_id=artifact_id,
            markdown=markdown,
            max_chunk_chars=request.max_chunk_chars,
            chunk_overlap_chars=request.chunk_overlap_chars,
            page_entries=page_entries,
            slide_entries=slide_entries,
            figure_entries=figure_entries,
            table_entries=table_entries,
            asset_entries=asset_entries,
        )
        document_json = self._export_json(
            document,
            result=result,
            source_path=file_path,
            source_filename=source_filename or file_path.name,
            artifact_id=artifact_id,
            mime_type=mime_type,
        )
        return ParsedDocument(
            title=self._extract_title(markdown, file_path),
            markdown=markdown,
            document_json=document_json,
            chunk_index=chunk_index,
            page_count=page_count if page_count is not None else (len(page_entries) or None),
            slide_count=slide_count if slide_count is not None else (len(slide_entries) or None),
            table_count=len(table_entries) or self._count_named_markers(markdown, "TABLE"),
            figure_count=len(figure_entries) or self._count_named_markers(markdown, "FIGURE"),
            section_count=len(chunk_index.get("sections") or []),
            asset_files=asset_files,
        )

    def _convert(self, *, file_path: Path, request: ParseRequest) -> tuple[Any, Any]:
        try:
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import (
                PdfPipelineOptions,
                PictureDescriptionVlmEngineOptions,
                VlmConvertOptions,
                VlmPipelineOptions,
            )
            from docling.datamodel.vlm_engine_options import ApiVlmEngineOptions, VlmEngineType
            from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption
            from docling.pipeline.vlm_pipeline import VlmPipeline
        except ImportError as exc:
            raise RuntimeError("Docling is not installed in the current runtime.") from exc
        try:
            from docling.datamodel.pipeline_options import TableFormerMode
        except ImportError:
            TableFormerMode = None
        try:
            from docling.datamodel.pipeline_options import PictureClassificationLabel
        except ImportError:
            PictureClassificationLabel = None

        format_options: dict[Any, Any] = {}
        pdf_like_suffixes = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
        if file_path.suffix.lower() in pdf_like_suffixes:
            if request.use_full_page_vlm and request.full_page_vlm is not None:
                pipeline_options = VlmPipelineOptions()
                if hasattr(pipeline_options, "enable_remote_services"):
                    pipeline_options.enable_remote_services = True
                if hasattr(pipeline_options, "generate_page_images"):
                    pipeline_options.generate_page_images = request.generate_page_images
                if hasattr(pipeline_options, "generate_picture_images"):
                    pipeline_options.generate_picture_images = request.generate_picture_images
                pipeline_options.vlm_options = VlmConvertOptions.from_preset(
                    request.full_page_vlm.preset,
                    engine_options=ApiVlmEngineOptions(
                        engine_type=VlmEngineType.API_OPENAI,
                        url=request.full_page_vlm.api_url,
                        headers={
                            "Authorization": f"Bearer {request.full_page_vlm.api_key}",
                        },
                        params=_openai_vlm_params(
                            model=request.full_page_vlm.model,
                            max_new_tokens=request.full_page_vlm.max_new_tokens,
                            reasoning_effort=request.full_page_vlm.reasoning_effort,
                        ),
                        timeout=request.full_page_vlm.timeout_sec,
                        concurrency=request.full_page_vlm.concurrency,
                    ),
                )
                vlm_options = getattr(pipeline_options, "vlm_options", None)
                if vlm_options is not None:
                    if hasattr(vlm_options, "batch_size"):
                        vlm_options.batch_size = request.full_page_vlm.batch_size
                    if hasattr(vlm_options, "scale"):
                        vlm_options.scale = request.full_page_vlm.scale
                    generation_config = getattr(vlm_options, "generation_config", None)
                    if isinstance(generation_config, dict):
                        generation_config.setdefault("do_sample", False)
                        generation_config["max_new_tokens"] = request.full_page_vlm.max_new_tokens
                format_options[InputFormat.PDF] = PdfFormatOption(
                    pipeline_cls=VlmPipeline,
                    pipeline_options=pipeline_options,
                )
            else:
                pipeline_options = PdfPipelineOptions()
                pipeline_options.do_ocr = request.enable_ocr
                pipeline_options.generate_page_images = request.generate_page_images
                pipeline_options.generate_picture_images = request.generate_picture_images
                if request.picture_description is not None:
                    pipeline_options.enable_remote_services = True
                    pipeline_options.do_picture_classification = True
                    pipeline_options.do_picture_description = True
                    pipeline_options.picture_description_options = PictureDescriptionVlmEngineOptions.from_preset(
                        request.picture_description.preset,
                        engine_options=ApiVlmEngineOptions(
                            engine_type=VlmEngineType.API_OPENAI,
                            url=request.picture_description.api_url,
                            headers={
                                "Authorization": f"Bearer {request.picture_description.api_key}",
                            },
                            params=_openai_vlm_params(
                                model=request.picture_description.model,
                                max_new_tokens=request.picture_description.max_new_tokens,
                                reasoning_effort=request.picture_description.reasoning_effort,
                            ),
                            timeout=request.picture_description.timeout_sec,
                            concurrency=request.picture_description.concurrency,
                        ),
                    )
                    picture_description_options = getattr(pipeline_options, "picture_description_options", None)
                    if picture_description_options is not None:
                        if hasattr(picture_description_options, "batch_size"):
                            picture_description_options.batch_size = request.picture_description.batch_size
                        if hasattr(picture_description_options, "scale"):
                            picture_description_options.scale = request.picture_description.scale
                        if hasattr(picture_description_options, "picture_area_threshold"):
                            picture_description_options.picture_area_threshold = request.picture_description.picture_area_threshold
                        if hasattr(picture_description_options, "classification_min_confidence"):
                            picture_description_options.classification_min_confidence = (
                                request.picture_description.classification_min_confidence
                            )
                        if hasattr(picture_description_options, "classification_deny"):
                            deny_labels: list[Any] = []
                            for raw_label in request.picture_description.classification_deny:
                                if PictureClassificationLabel is None:
                                    deny_labels.append(raw_label)
                                    continue
                                try:
                                    deny_labels.append(PictureClassificationLabel(raw_label))
                                except Exception:
                                    deny_labels.append(raw_label)
                            picture_description_options.classification_deny = deny_labels
                        if hasattr(picture_description_options, "prompt"):
                            picture_description_options.prompt = request.picture_description.prompt
                        generation_config = getattr(picture_description_options, "generation_config", None)
                        if isinstance(generation_config, dict):
                            generation_config.setdefault("do_sample", False)
                            generation_config["max_new_tokens"] = request.picture_description.max_new_tokens
                if hasattr(pipeline_options, "do_table_structure"):
                    pipeline_options.do_table_structure = True
                table_structure_options = getattr(pipeline_options, "table_structure_options", None)
                if TableFormerMode is not None and table_structure_options is not None and hasattr(table_structure_options, "mode"):
                    try:
                        table_structure_options.mode = TableFormerMode.ACCURATE
                    except Exception:
                        pass
                format_options[InputFormat.PDF] = PdfFormatOption(pipeline_options=pipeline_options)
                format_options[InputFormat.IMAGE] = ImageFormatOption(pipeline_options=pipeline_options)

        converter = DocumentConverter(**({"format_options": format_options} if format_options else {}))
        try:
            result = converter.convert(
                str(file_path),
                max_num_pages=request.max_num_pages,
                max_file_size=request.max_file_size_bytes,
            )
        except Exception as exc:
            message = str(exc).strip() or f"Docling failed to parse {file_path.name}."
            raise RuntimeError(message) from exc
        document = getattr(result, "document", None)
        if document is None:
            raise RuntimeError("Docling conversion did not return a document object.")
        return document, result

    def _page_label(self, *, mime_type: str, file_path: Path) -> str:
        normalized_mime = str(mime_type or "").strip().lower()
        if "presentation" in normalized_mime or file_path.suffix.lower() in {".ppt", ".pptx"}:
            return "SLIDE"
        return "PAGE"

    def _export_markdown(self, document: Any, *, page_break_placeholder: str | None = None, page_no: int | None = None) -> str:
        if hasattr(document, "export_to_markdown"):
            variants: list[dict[str, Any]] = []
            if page_break_placeholder is not None and page_no is not None:
                variants.append({"page_break_placeholder": page_break_placeholder, "page_no": page_no})
            if page_break_placeholder is not None:
                variants.append({"page_break_placeholder": page_break_placeholder})
            if page_no is not None:
                variants.append({"page_no": page_no})
            variants.append({})
            for kwargs in variants:
                try:
                    rendered = document.export_to_markdown(**kwargs)
                except TypeError:
                    continue
                if isinstance(rendered, str):
                    return rendered
        return json.dumps(self._coerce_json(document), ensure_ascii=False, indent=2)

    def _build_page_segments(self, *, document: Any, fallback_markdown: str, fallback_count: int | None) -> list[dict[str, Any]]:
        page_numbers = self._extract_page_numbers(document, fallback_count=fallback_count)
        if not page_numbers:
            return []
        joined = self._export_markdown(document, page_break_placeholder=PAGE_BREAK_PLACEHOLDER)
        if PAGE_BREAK_PLACEHOLDER in joined:
            pieces = [piece.strip() for piece in joined.split(PAGE_BREAK_PLACEHOLDER)]
            if len(pieces) == len(page_numbers):
                return [{"page_number": page_no, "text": piece} for page_no, piece in zip(page_numbers, pieces, strict=False)]
        segments: list[dict[str, Any]] = []
        for page_no in page_numbers:
            piece = self._export_markdown(document, page_no=page_no).strip()
            if not piece:
                segments = []
                break
            segments.append({"page_number": page_no, "text": piece})
        if segments:
            return segments
        if len(page_numbers) == 1 and fallback_markdown.strip():
            return [{"page_number": page_numbers[0], "text": fallback_markdown.strip()}]
        return []

    def _extract_page_numbers(self, document: Any, *, fallback_count: int | None) -> list[int]:
        pages = getattr(document, "pages", None)
        if isinstance(pages, dict):
            values = []
            for key in pages:
                try:
                    values.append(int(key))
                except (TypeError, ValueError):
                    continue
            return sorted({value for value in values if value > 0})
        if isinstance(pages, (list, tuple)) and pages:
            return list(range(1, len(pages) + 1))
        if isinstance(fallback_count, int) and fallback_count > 0:
            return list(range(1, fallback_count + 1))
        return []

    def _compose_document_markdown(
        self,
        *,
        fallback_markdown: str,
        page_segments: list[dict[str, Any]],
        page_label: str,
        figure_entries: list[dict[str, Any]],
        table_entries: list[dict[str, Any]],
        page_image_by_number: dict[int, dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        figures = [dict(item) for item in figure_entries]
        tables = [dict(item) for item in table_entries]
        if not page_segments:
            markdown = fallback_markdown.strip()
            return markdown, [], figures, tables

        rendered_parts: list[str] = []
        page_entries: list[dict[str, Any]] = []
        cursor = 0
        for index, segment in enumerate(page_segments):
            page_number = int(segment["page_number"])
            page_markers: list[str] = [f"[{page_label} {page_number}]"]
            for entry in figures:
                if entry.get("page_number") == page_number:
                    marker = self._marker_line("FIGURE", entry["figure_id"], entry.get("asset_id"), page_number, "caption", entry.get("caption"))
                    existing = "\n\n".join(page_markers)
                    entry["start_char"] = cursor + len(existing) + (2 if existing else 0)
                    entry["end_char"] = entry["start_char"] + len(marker)
                    entry["anchor_id"] = entry["figure_id"]
                    entry["marker_text"] = marker
                    page_markers.append(marker)
            for entry in tables:
                if entry.get("page_number") == page_number:
                    marker = self._marker_line("TABLE", entry["table_id"], entry.get("asset_id"), page_number, "title", entry.get("title"))
                    existing = "\n\n".join(page_markers)
                    entry["start_char"] = cursor + len(existing) + (2 if existing else 0)
                    entry["end_char"] = entry["start_char"] + len(marker)
                    entry["anchor_id"] = entry["table_id"]
                    entry["marker_text"] = marker
                    page_markers.append(marker)
            if str(segment.get("text") or "").strip():
                page_markers.append(str(segment["text"]).strip())
            block = "\n\n".join(page_markers).strip()
            start_char = cursor
            end_char = cursor + len(block)
            page_entry = {
                "page_id": f"page_{page_number:04d}",
                "page_number": page_number,
                "start_char": start_char,
                "end_char": end_char,
                "anchor_id": f"page_{page_number:04d}",
            }
            if page_number in page_image_by_number:
                page_entry.update(page_image_by_number[page_number])
            rendered_parts.append(block)
            page_entries.append(page_entry)
            cursor = end_char + (2 if index + 1 < len(page_segments) else 0)
        return "\n\n".join(rendered_parts).strip(), page_entries, figures, tables

    def _marker_line(
        self,
        kind: str,
        logical_id: str,
        asset_id: Any,
        page_number: int,
        text_label: str,
        text_value: Any,
    ) -> str:
        parts = [f"id={logical_id}"]
        if asset_id:
            parts.append(f"asset_id={asset_id}")
        parts.append(f"page={page_number}")
        text = str(text_value or "").strip().replace("\\", "\\\\").replace('"', '\\"')
        if text:
            parts.append(f'{text_label}="{text}"')
        return f"[{kind} {' '.join(parts)}]"

    def _extract_assets(self, *, document: Any, request: ParseRequest) -> tuple[
        list[tuple[str, bytes, str]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[int, dict[str, Any]],
    ]:
        asset_files: list[tuple[str, bytes, str]] = []
        asset_entries: list[dict[str, Any]] = []
        figure_entries: list[dict[str, Any]] = []
        table_entries: list[dict[str, Any]] = []
        page_image_by_number: dict[int, dict[str, Any]] = {}

        for page_number, page_item in self._iter_page_items(document):
            if not request.generate_page_images:
                continue
            image_bytes = self._image_to_png_bytes(getattr(page_item, "image", None))
            if image_bytes is None:
                continue
            asset_id = f"asset_page_{self._stable_id(f'page:{page_number}')}"
            relative_path = f"assets/pages/page_{page_number:04d}.png"
            asset_files.append((relative_path, image_bytes, "image/png"))
            entry = {"asset_id": asset_id, "kind": "page_image", "page_number": page_number, "path": relative_path, "mime": "image/png"}
            asset_entries.append(entry)
            page_image_by_number[page_number] = entry

        for index, item in enumerate(self._iter_collection(document, "pictures"), start=1):
            page_number = self._extract_page_number(item)
            caption = self._extract_caption(item, document) or None
            description = self._extract_picture_description(item)
            classification = self._extract_picture_classification(item)
            self_ref = str(getattr(item, "self_ref", "") or "")
            figure_id = f"fig_{self._stable_id(f'figure:{index}:{caption}:{description}:{page_number}:{self_ref}')}"
            asset_id = None
            image_bytes = self._image_to_png_bytes(self._extract_item_image(item, document))
            if image_bytes is not None:
                asset_id = f"asset_fig_{self._stable_id(f'{figure_id}:png')}"
                relative_path = f"assets/figures/{figure_id}.png"
                asset_files.append((relative_path, image_bytes, "image/png"))
                asset_entries.append(
                    {
                        "asset_id": asset_id,
                        "kind": "figure_image",
                        "figure_id": figure_id,
                        "page_number": page_number,
                        "path": relative_path,
                        "mime": "image/png",
                        "description": description,
                        "classification": classification,
                    }
                )
            figure_entries.append(
                {
                    "figure_id": figure_id,
                    "asset_id": asset_id,
                    "caption": caption,
                    "description": description,
                    "classification": classification,
                    "page_number": page_number,
                }
            )

        for index, item in enumerate(self._iter_collection(document, "tables"), start=1):
            page_number = self._extract_page_number(item)
            title = self._extract_caption(item, document) or f"Table {index}"
            self_ref = str(getattr(item, "self_ref", "") or "")
            table_id = f"tbl_{self._stable_id(f'table:{index}:{title}:{page_number}:{self_ref}')}"
            asset_id = None
            markdown_text = self._export_table_text(item, document, method_name="export_to_markdown")
            if markdown_text:
                asset_id = f"asset_tbl_{self._stable_id(f'{table_id}:md')}"
                relative_path = f"assets/tables/{table_id}.md"
                asset_files.append((relative_path, markdown_text.encode("utf-8"), "text/markdown"))
                asset_entries.append(
                    {"asset_id": asset_id, "kind": "table_markdown", "table_id": table_id, "page_number": page_number, "path": relative_path, "mime": "text/markdown"}
                )
            table_entries.append(
                {
                    "table_id": table_id,
                    "asset_id": asset_id,
                    "title": title,
                    "page_number": page_number,
                    "text_excerpt": self._bounded_excerpt(markdown_text, limit=400) if markdown_text else None,
                }
            )
        return asset_files, asset_entries, figure_entries, table_entries, page_image_by_number

    def _iter_collection(self, document: Any, attr_name: str) -> list[Any]:
        value = getattr(document, attr_name, None)
        return list(value) if isinstance(value, (list, tuple)) else []

    def _iter_page_items(self, document: Any) -> list[tuple[int, Any]]:
        pages = getattr(document, "pages", None)
        if isinstance(pages, dict):
            items: list[tuple[int, Any]] = []
            for key, value in pages.items():
                try:
                    page_number = int(key)
                except (TypeError, ValueError):
                    continue
                if page_number > 0:
                    items.append((page_number, value))
            return sorted(items, key=lambda item: item[0])
        if isinstance(pages, (list, tuple)):
            return [(index, value) for index, value in enumerate(pages, start=1)]
        return []

    def _extract_item_image(self, item: Any, document: Any) -> Any | None:
        get_image = getattr(item, "get_image", None)
        if callable(get_image):
            try:
                return get_image(document)
            except Exception:
                return None
        return getattr(item, "image", None)

    def _image_to_png_bytes(self, value: Any) -> bytes | None:
        if value is None:
            return None
        image = value if hasattr(value, "save") else getattr(value, "pil_image", None)
        if image is None or not hasattr(image, "save"):
            return None
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def _extract_caption(self, item: Any, document: Any) -> str:
        for name in ("caption_text", "get_caption_text"):
            candidate = getattr(item, name, None)
            if callable(candidate):
                for args in ((document,), tuple()):
                    try:
                        text = str(candidate(*args) or "").strip()
                    except TypeError:
                        continue
                    if text:
                        return text
        caption = getattr(item, "caption", None)
        return self._flatten_text(caption)

    def _extract_picture_description(self, item: Any) -> str | None:
        meta = getattr(item, "meta", None)
        if meta is None:
            return None
        description = getattr(meta, "description", None)
        text = self._flatten_text(getattr(description, "text", None))
        return text or None

    def _extract_picture_classification(self, item: Any) -> dict[str, Any] | None:
        meta = getattr(item, "meta", None)
        if meta is None:
            return None
        classification = getattr(meta, "classification", None)
        predictions = getattr(classification, "predictions", None)
        if not isinstance(predictions, list) or not predictions:
            return None
        best: dict[str, Any] | None = None
        for prediction in predictions:
            class_name = self._flatten_text(getattr(prediction, "class_name", None))
            confidence = getattr(prediction, "confidence", None)
            if not class_name:
                continue
            candidate = {
                "label": class_name,
                "confidence": float(confidence) if isinstance(confidence, (int, float)) else None,
            }
            if best is None or (
                candidate["confidence"] is not None
                and (best.get("confidence") is None or candidate["confidence"] > best["confidence"])
            ):
                best = candidate
        return best

    def _export_table_text(self, item: Any, document: Any, *, method_name: str) -> str:
        method = getattr(item, method_name, None)
        if not callable(method):
            return ""
        for kwargs in ({"doc": document}, {"document": document}, {}):
            try:
                text = str(method(**kwargs) or "").strip()
            except TypeError:
                continue
            if text:
                return text
        return ""

    def _extract_page_number(self, item: Any) -> int | None:
        direct = getattr(item, "page_no", None)
        try:
            if direct is not None:
                value = int(direct)
                if value > 0:
                    return value
        except (TypeError, ValueError):
            pass
        provenance = getattr(item, "prov", None)
        if isinstance(provenance, list):
            for prov_item in provenance:
                if prov_item is None:
                    continue
                for key in ("page_no", "page_number"):
                    value = getattr(prov_item, key, None)
                    if value is None and isinstance(prov_item, dict):
                        value = prov_item.get(key)
                    try:
                        if value is not None:
                            number = int(value)
                            if number > 0:
                                return number
                    except (TypeError, ValueError):
                        continue
        return None

    def _flatten_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            return " ".join(part for part in (self._flatten_text(item) for item in value.values()) if part).strip()
        if isinstance(value, (list, tuple)):
            return " ".join(part for part in (self._flatten_text(item) for item in value) if part).strip()
        return str(value).strip()

    def _export_json(
        self,
        document: Any,
        *,
        result: Any,
        source_path: Path,
        source_filename: str,
        artifact_id: str,
        mime_type: str,
    ) -> dict[str, Any]:
        payload = self._coerce_json(document)
        if not isinstance(payload, dict):
            payload = {"document": payload}
        payload.setdefault("parser", {})
        payload["parser"].update(
            {
                "name": "docling",
                "source_filename": source_filename,
                "converter_source_filename": source_path.name,
                "artifact_id": artifact_id,
                "mime_type": mime_type,
            }
        )
        if result is not None and hasattr(result, "__dict__"):
            payload["parse_result_meta"] = {key: self._coerce_json(value) for key, value in result.__dict__.items() if key != "document"}
        return payload

    def _coerce_json(self, value: Any) -> Any:
        if hasattr(value, "export_to_dict"):
            try:
                return value.export_to_dict()
            except Exception:
                pass
        if hasattr(value, "model_dump"):
            try:
                return value.model_dump(mode="json")
            except Exception:
                pass
        if hasattr(value, "dict"):
            try:
                return value.dict()
            except Exception:
                pass
        if hasattr(value, "__dict__"):
            return {key: self._coerce_json(item) for key, item in value.__dict__.items() if not key.startswith("_")}
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, list):
            return [self._coerce_json(item) for item in value]
        if isinstance(value, tuple):
            return [self._coerce_json(item) for item in value]
        if isinstance(value, dict):
            return {str(key): self._coerce_json(item) for key, item in value.items()}
        return str(value)

    def _extract_title(self, markdown: str, file_path: Path) -> str | None:
        for line in markdown.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip() or file_path.stem
        return file_path.stem

    def _extract_optional_int(self, result: Any, attr_name: str) -> int | None:
        value = getattr(result, attr_name, None)
        return value if isinstance(value, int) and value >= 0 else None

    def _count_named_markers(self, markdown: str, marker_name: str) -> int:
        return len(re.findall(r"\[" + re.escape(marker_name) + r"\b", markdown))

    def _build_chunk_index(
        self,
        *,
        artifact_id: str,
        markdown: str,
        max_chunk_chars: int,
        chunk_overlap_chars: int,
        page_entries: list[dict[str, Any]],
        slide_entries: list[dict[str, Any]],
        figure_entries: list[dict[str, Any]],
        table_entries: list[dict[str, Any]],
        asset_entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        sections = self._build_sections(markdown)
        chunks: list[dict[str, Any]] = []
        for section in sections:
            chunks.extend(
                self._chunk_section(
                    artifact_id=artifact_id,
                    markdown=markdown,
                    section=section,
                    max_chunk_chars=max_chunk_chars,
                    chunk_overlap_chars=chunk_overlap_chars,
                )
            )
        for index, chunk in enumerate(chunks):
            chunk["prev_chunk_id"] = chunks[index - 1]["chunk_id"] if index > 0 else None
            chunk["next_chunk_id"] = chunks[index + 1]["chunk_id"] if index + 1 < len(chunks) else None
            chunk["page_numbers"] = self._collect_number_refs(page_entries, chunk.get("doc_start_char"), chunk.get("doc_end_char"), "page_number")
            chunk["slide_numbers"] = self._collect_number_refs(slide_entries, chunk.get("doc_start_char"), chunk.get("doc_end_char"), "slide_number")
        for section in sections:
            section["page_numbers"] = self._collect_number_refs(page_entries, section.get("start_char"), section.get("end_char"), "page_number")
            section["slide_numbers"] = self._collect_number_refs(slide_entries, section.get("start_char"), section.get("end_char"), "slide_number")
            section["search_text"] = self._build_search_text(
                title=str(section.get("title") or ""),
                body=str(section.get("text") or ""),
                page_numbers=section.get("page_numbers"),
                slide_numbers=section.get("slide_numbers"),
                overlapping_figures=self._collect_overlapping_entries(figure_entries, section.get("start_char"), section.get("end_char")),
                overlapping_tables=self._collect_overlapping_entries(table_entries, section.get("start_char"), section.get("end_char")),
            )
        for chunk in chunks:
            chunk["search_text"] = self._build_search_text(
                title=str(chunk.get("section_title") or ""),
                body=str(chunk.get("text") or ""),
                page_numbers=chunk.get("page_numbers"),
                slide_numbers=chunk.get("slide_numbers"),
                overlapping_figures=self._collect_overlapping_entries(figure_entries, chunk.get("doc_start_char"), chunk.get("doc_end_char")),
                overlapping_tables=self._collect_overlapping_entries(table_entries, chunk.get("doc_start_char"), chunk.get("doc_end_char")),
            )
        return {
            "sections": sections,
            "chunks": chunks,
            "pages": page_entries,
            "slides": slide_entries,
            "tables": table_entries,
            "figures": figure_entries,
            "assets": asset_entries,
            "chunk_count": len(chunks),
            "section_count": len(sections),
            "page_count": len(page_entries),
            "slide_count": len(slide_entries),
            "table_count": len(table_entries),
            "figure_count": len(figure_entries),
            "asset_count": len(asset_entries),
        }

    def _build_sections(self, markdown: str) -> list[dict[str, Any]]:
        lines = markdown.splitlines(keepends=True)
        sections: list[dict[str, Any]] = []
        current_title = "Document Start"
        current_level = 0
        current_lines: list[str] = []
        current_start = 0
        current_end = 0
        section_index = 0
        offset = 0

        def flush() -> None:
            nonlocal section_index, current_lines
            raw_text = "".join(current_lines)
            text = raw_text.strip()
            if not text:
                current_lines = []
                return
            section_index += 1
            leading = len(raw_text) - len(raw_text.lstrip())
            trailing = len(raw_text) - len(raw_text.rstrip())
            section_id = self._stable_id(f"section:{section_index}:{current_title}:{text[:120]}")
            sections.append(
                {
                    "section_id": section_id,
                    "index": section_index,
                    "title": current_title,
                    "level": current_level,
                    "text": text,
                    "start_char": current_start + leading,
                    "end_char": max(current_start + leading, current_end - trailing),
                    "anchor_id": section_id,
                }
            )
            current_lines = []

        for raw_line in lines:
            line_start = offset
            offset += len(raw_line)
            heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", raw_line.strip())
            if heading:
                flush()
                current_title = heading.group(2).strip()
                current_level = len(heading.group(1))
                current_lines = [raw_line]
                current_start = line_start
                current_end = offset
                continue
            if not current_lines:
                current_start = line_start
            current_lines.append(raw_line)
            current_end = offset
        flush()

        if not sections and markdown.strip():
            stripped = markdown.strip()
            leading = len(markdown) - len(markdown.lstrip())
            trailing = len(markdown) - len(markdown.rstrip())
            section_id = self._stable_id(f"section:1:{stripped[:120]}")
            sections.append(
                {
                    "section_id": section_id,
                    "index": 1,
                    "title": "Document",
                    "level": 0,
                    "text": stripped,
                    "start_char": leading,
                    "end_char": max(leading, len(markdown) - trailing),
                    "anchor_id": section_id,
                }
            )
        return sections

    def _chunk_section(
        self,
        *,
        artifact_id: str,
        markdown: str,
        section: dict[str, Any],
        max_chunk_chars: int,
        chunk_overlap_chars: int,
    ) -> list[dict[str, Any]]:
        section_text = str(section.get("text") or "").strip()
        if not section_text:
            return []
        section_start = int(section.get("start_char") or 0)
        section_end = int(section.get("end_char") or 0)
        section_markdown = markdown[section_start:section_end] if section_end > section_start else section_text
        blocks = self._build_structural_blocks(
            section_markdown=section_markdown,
            section_start=section_start,
            max_chunk_chars=max_chunk_chars,
        )
        if not blocks:
            blocks = [
                {
                    "text": section_text,
                    "start_char": section_start,
                    "end_char": section_end if section_end > section_start else section_start + len(section_text),
                    "kind": "text",
                }
            ]
        chunks: list[dict[str, Any]] = []
        current_blocks: list[dict[str, Any]] = []
        current_chars = 0
        part = 0

        def flush(blocks_to_emit: list[dict[str, Any]]) -> None:
            nonlocal part
            if not blocks_to_emit:
                return
            chunk = self._emit_chunk_from_blocks(
                artifact_id=artifact_id,
                section=section,
                markdown=markdown,
                blocks=blocks_to_emit,
                part=part + 1,
                section_start=section_start,
            )
            if chunk is None:
                return
            if chunks and int(chunks[-1].get("doc_start_char") or -1) == int(chunk.get("doc_start_char") or -2) and int(chunks[-1].get("doc_end_char") or -1) == int(chunk.get("doc_end_char") or -2):
                return
            part += 1
            chunks.append(chunk)

        for block in blocks:
            block_len = len(str(block.get("text") or ""))
            separator_len = 2 if current_blocks else 0
            if current_blocks and current_chars + separator_len + block_len > max_chunk_chars:
                emitted_blocks = list(current_blocks)
                flush(emitted_blocks)
                overlap_blocks: list[dict[str, Any]] = []
                overlap_chars = 0
                for tail_block in reversed(emitted_blocks):
                    overlap_blocks.insert(0, tail_block)
                    overlap_chars += len(str(tail_block.get("text") or "")) + (2 if overlap_blocks[:-1] else 0)
                    if overlap_chars >= chunk_overlap_chars:
                        break
                current_blocks = overlap_blocks
                current_chars = sum(len(str(item.get("text") or "")) for item in current_blocks) + max(0, (len(current_blocks) - 1) * 2)
                separator_len = 2 if current_blocks else 0
                if current_blocks and current_chars + separator_len + block_len > max_chunk_chars and len(current_blocks) == 1:
                    current_blocks = []
                    current_chars = 0
            current_blocks.append(block)
            current_chars += block_len + (2 if len(current_blocks) > 1 else 0)
        flush(current_blocks)
        return chunks

    def _build_structural_blocks(self, *, section_markdown: str, section_start: int, max_chunk_chars: int) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for match in re.finditer(r"\S[\s\S]*?(?:(?:\n\s*\n)+|$)", section_markdown):
            raw_block = match.group(0)
            leading = len(raw_block) - len(raw_block.lstrip())
            trailing = len(raw_block) - len(raw_block.rstrip())
            text = raw_block.strip()
            if not text:
                continue
            absolute_start = section_start + match.start() + leading
            absolute_end = section_start + match.end() - trailing
            blocks.extend(
                self._split_large_block(
                    text=text,
                    absolute_start=absolute_start,
                    max_chunk_chars=max_chunk_chars,
                    kind=self._classify_block(text),
                )
            )
        return blocks

    def _split_large_block(self, *, text: str, absolute_start: int, max_chunk_chars: int, kind: str) -> list[dict[str, Any]]:
        if len(text) <= max_chunk_chars:
            return [{"text": text, "start_char": absolute_start, "end_char": absolute_start + len(text), "kind": kind}]
        blocks: list[dict[str, Any]] = []
        start = 0
        minimum_chunk_chars = max(240, max_chunk_chars // 2)
        while start < len(text):
            hard_end = min(len(text), start + max_chunk_chars)
            if hard_end >= len(text):
                end = len(text)
            else:
                end = self._find_preferred_boundary(text=text, start=start, hard_end=hard_end, minimum_end=min(len(text), start + minimum_chunk_chars))
            if end <= start:
                end = hard_end
            slice_text = text[start:end].strip()
            if not slice_text:
                break
            leading = len(text[start:end]) - len(text[start:end].lstrip())
            trailing = len(text[start:end]) - len(text[start:end].rstrip())
            block_start = absolute_start + start + leading
            block_end = absolute_start + end - trailing
            blocks.append({"text": slice_text, "start_char": block_start, "end_char": block_end, "kind": kind})
            start = end
        return blocks

    def _find_preferred_boundary(self, *, text: str, start: int, hard_end: int, minimum_end: int) -> int:
        window = text[start:hard_end]
        candidate_patterns = [
            r"(?s).*(?:\n\s*\n)",
            r"(?s).*[.!?]\s+",
            r"(?s).*[:;]\s+",
            r"(?s).*,\s+",
            r"(?s).*\s+",
        ]
        for pattern in candidate_patterns:
            match = re.match(pattern, window)
            if match is None:
                continue
            candidate_end = start + match.end()
            if candidate_end >= minimum_end:
                return candidate_end
        return hard_end

    def _emit_chunk_from_blocks(
        self,
        *,
        artifact_id: str,
        section: dict[str, Any],
        markdown: str,
        blocks: list[dict[str, Any]],
        part: int,
        section_start: int,
    ) -> dict[str, Any] | None:
        if not blocks:
            return None
        try:
            doc_start = min(int(item.get("start_char")) for item in blocks)
            doc_end = max(int(item.get("end_char")) for item in blocks)
        except (TypeError, ValueError):
            return None
        if doc_end <= doc_start:
            return None
        slice_text = markdown[doc_start:doc_end].strip()
        if not slice_text:
            return None
        chunk_id = self._stable_id(f"{artifact_id}:{section['section_id']}:{part}:{slice_text[:160]}")
        return {
            "chunk_id": chunk_id,
            "artifact_id": artifact_id,
            "section_id": section["section_id"],
            "section_title": section["title"],
            "chunk_index_within_section": part,
            "start_char": max(0, doc_start - section_start),
            "end_char": max(0, doc_end - section_start),
            "doc_start_char": doc_start,
            "doc_end_char": doc_end,
            "text": slice_text,
            "estimated_chars": len(slice_text),
            "anchor_id": chunk_id,
            "block_count": len(blocks),
            "block_kinds": [str(item.get("kind") or "text") for item in blocks],
        }

    def _classify_block(self, text: str) -> str:
        stripped = text.strip()
        if re.match(r"^#{1,6}\s+", stripped):
            return "heading"
        if stripped.startswith("[PAGE ") or stripped.startswith("[SLIDE "):
            return "page_marker"
        if stripped.startswith("[TABLE "):
            return "table_marker"
        if stripped.startswith("[FIGURE "):
            return "figure_marker"
        return "text"

    def _collect_overlapping_entries(self, entries: list[dict[str, Any]], start_char: Any, end_char: Any) -> list[dict[str, Any]]:
        try:
            start = int(start_char)
            end = int(end_char)
        except (TypeError, ValueError):
            return []
        selected: list[dict[str, Any]] = []
        for entry in entries:
            try:
                entry_start = int(entry.get("start_char"))
                entry_end = int(entry.get("end_char"))
            except (TypeError, ValueError):
                continue
            if entry_end <= start or entry_start >= end:
                continue
            selected.append(entry)
        return selected

    def _build_search_text(
        self,
        *,
        title: str,
        body: str,
        page_numbers: Any,
        slide_numbers: Any,
        overlapping_figures: list[dict[str, Any]],
        overlapping_tables: list[dict[str, Any]],
    ) -> str:
        parts: list[str] = []
        clean_title = self._flatten_text(title)
        clean_body = self._flatten_text(body)
        if clean_title:
            parts.append(clean_title)
        if isinstance(page_numbers, list) and page_numbers:
            parts.append("pages " + " ".join(str(int(number)) for number in page_numbers if isinstance(number, int)))
        if isinstance(slide_numbers, list) and slide_numbers:
            parts.append("slides " + " ".join(str(int(number)) for number in slide_numbers if isinstance(number, int)))
        for figure in overlapping_figures:
            caption = self._flatten_text(figure.get("caption"))
            description = self._flatten_text(figure.get("description"))
            classification = figure.get("classification") if isinstance(figure.get("classification"), dict) else {}
            label = self._flatten_text(classification.get("label"))
            figure_bits = [bit for bit in (caption, description, label) if bit]
            if figure_bits:
                parts.append("figure " + " ".join(figure_bits))
        for table in overlapping_tables:
            table_title = self._flatten_text(table.get("title"))
            table_excerpt = self._flatten_text(table.get("text_excerpt"))
            table_bits = [bit for bit in (table_title, table_excerpt) if bit]
            if table_bits:
                parts.append("table " + " ".join(table_bits))
        if clean_body:
            parts.append(clean_body)
        return " ".join(part for part in parts if part).strip()

    def _bounded_excerpt(self, value: Any, *, limit: int) -> str:
        text = self._flatten_text(value)
        if len(text) <= limit:
            return text
        return f"{text[: limit - 3].rstrip()}..."

    def _collect_number_refs(self, entries: list[dict[str, Any]], start_char: Any, end_char: Any, number_key: str) -> list[int]:
        try:
            start = int(start_char)
            end = int(end_char)
        except (TypeError, ValueError):
            return []
        values: list[int] = []
        for entry in entries:
            try:
                entry_start = int(entry.get("start_char"))
                entry_end = int(entry.get("end_char"))
                number = int(entry.get(number_key))
            except (TypeError, ValueError):
                continue
            if entry_end <= start or entry_start >= end:
                continue
            values.append(number)
        return sorted(set(values))

    def _stable_id(self, raw: str) -> str:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
