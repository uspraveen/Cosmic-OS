from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shared import SUPPORTED_DOCUMENT_EXTENSIONS, SUPPORTED_DOCUMENT_MIME_TYPES

SUPPORTED_MIME_TYPES = SUPPORTED_DOCUMENT_MIME_TYPES
SUPPORTED_EXTENSIONS = SUPPORTED_DOCUMENT_EXTENSIONS
PAGE_BREAK_PLACEHOLDER = "\n\n[[DOCLING_PAGE_BREAK]]\n\n"


@dataclass(slots=True)
class ParseRequest:
    enable_ocr: bool
    generate_page_images: bool
    generate_picture_images: bool
    max_chunk_chars: int
    chunk_overlap_chars: int


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
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.document_converter import DocumentConverter, PdfFormatOption
        except ImportError as exc:
            raise RuntimeError("Docling is not installed in the current runtime.") from exc

        format_options: dict[Any, Any] = {}
        if file_path.suffix.lower() == ".pdf":
            pipeline_options = PdfPipelineOptions()
            pipeline_options.do_ocr = request.enable_ocr
            pipeline_options.generate_page_images = request.generate_page_images
            pipeline_options.generate_picture_images = request.generate_picture_images
            format_options[InputFormat.PDF] = PdfFormatOption(pipeline_options=pipeline_options)

        converter = DocumentConverter(**({"format_options": format_options} if format_options else {}))
        result = converter.convert(str(file_path))
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
            self_ref = str(getattr(item, "self_ref", "") or "")
            figure_id = f"fig_{self._stable_id(f'figure:{index}:{caption}:{page_number}:{self_ref}')}"
            asset_id = None
            image_bytes = self._image_to_png_bytes(self._extract_item_image(item, document))
            if image_bytes is not None:
                asset_id = f"asset_fig_{self._stable_id(f'{figure_id}:png')}"
                relative_path = f"assets/figures/{figure_id}.png"
                asset_files.append((relative_path, image_bytes, "image/png"))
                asset_entries.append(
                    {"asset_id": asset_id, "kind": "figure_image", "figure_id": figure_id, "page_number": page_number, "path": relative_path, "mime": "image/png"}
                )
            figure_entries.append({"figure_id": figure_id, "asset_id": asset_id, "caption": caption, "page_number": page_number})

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
            table_entries.append({"table_id": table_id, "asset_id": asset_id, "title": title, "page_number": page_number})
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

    def _export_json(self, document: Any, *, result: Any, source_path: Path, artifact_id: str, mime_type: str) -> dict[str, Any]:
        payload = self._coerce_json(document)
        if not isinstance(payload, dict):
            payload = {"document": payload}
        payload.setdefault("parser", {})
        payload["parser"].update(
            {
                "name": "docling",
                "source_filename": source_path.name,
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

    def _chunk_section(self, *, artifact_id: str, section: dict[str, Any], max_chunk_chars: int, chunk_overlap_chars: int) -> list[dict[str, Any]]:
        text = str(section.get("text") or "").strip()
        if not text:
            return []
        chunks: list[dict[str, Any]] = []
        start = 0
        part = 0
        section_start = int(section.get("start_char") or 0)
        while start < len(text):
            end = min(len(text), start + max_chunk_chars)
            slice_text = text[start:end].strip()
            if not slice_text:
                break
            part += 1
            chunk_id = self._stable_id(f"{artifact_id}:{section['section_id']}:{part}:{slice_text[:160]}")
            chunks.append(
                {
                    "chunk_id": chunk_id,
                    "artifact_id": artifact_id,
                    "section_id": section["section_id"],
                    "section_title": section["title"],
                    "chunk_index_within_section": part,
                    "start_char": start,
                    "end_char": end,
                    "doc_start_char": section_start + start,
                    "doc_end_char": section_start + end,
                    "text": slice_text,
                    "estimated_chars": len(slice_text),
                    "anchor_id": chunk_id,
                }
            )
            if end >= len(text):
                break
            start = max(end - chunk_overlap_chars, start + 1)
        return chunks

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
