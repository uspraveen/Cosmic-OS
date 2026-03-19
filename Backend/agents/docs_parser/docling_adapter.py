from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shared import (
    SUPPORTED_DOCUMENT_EXTENSIONS,
    SUPPORTED_DOCUMENT_MIME_TYPES,
)

SUPPORTED_MIME_TYPES = SUPPORTED_DOCUMENT_MIME_TYPES
SUPPORTED_EXTENSIONS = SUPPORTED_DOCUMENT_EXTENSIONS


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
        markdown = self._export_markdown(document)
        json_payload = self._export_json(
            document,
            result=result,
            source_path=file_path,
            artifact_id=artifact_id,
            mime_type=mime_type,
        )
        chunk_index = self._build_chunk_index(
            artifact_id=artifact_id,
            markdown=markdown,
            max_chunk_chars=request.max_chunk_chars,
            chunk_overlap_chars=request.chunk_overlap_chars,
        )
        title = self._extract_title(markdown, file_path)
        page_count = self._extract_optional_int(result, "page_count")
        slide_count = self._extract_optional_int(result, "slide_count")
        table_count = self._count_named_markers(markdown, "TABLE")
        figure_count = self._count_named_markers(markdown, "FIGURE")
        section_count = len(chunk_index.get("sections") or [])
        return ParsedDocument(
            title=title,
            markdown=markdown,
            document_json=json_payload,
            chunk_index=chunk_index,
            page_count=page_count,
            slide_count=slide_count,
            table_count=table_count,
            figure_count=figure_count,
            section_count=section_count,
            asset_files=[],
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

        converter_kwargs = {"format_options": format_options} if format_options else {}
        converter = DocumentConverter(**converter_kwargs)
        result = converter.convert(str(file_path))
        document = getattr(result, "document", None)
        if document is None:
            raise RuntimeError("Docling conversion did not return a document object.")
        return document, result

    def _export_markdown(self, document: Any) -> str:
        if hasattr(document, "export_to_markdown"):
            markdown = document.export_to_markdown()
            if isinstance(markdown, str) and markdown.strip():
                return markdown
        text = json.dumps(self._coerce_json(document), ensure_ascii=False, indent=2)
        return text

    def _export_json(
        self,
        document: Any,
        *,
        result: Any,
        source_path: Path,
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
                "source_filename": source_path.name,
                "artifact_id": artifact_id,
                "mime_type": mime_type,
            }
        )
        if result is not None and hasattr(result, "__dict__"):
            payload["parse_result_meta"] = {
                key: self._coerce_json(value)
                for key, value in result.__dict__.items()
                if key not in {"document"}
            }
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
        if isinstance(value, int) and value >= 0:
            return value
        return None

    def _count_named_markers(self, markdown: str, marker_name: str) -> int:
        pattern = r"\[" + re.escape(marker_name) + r"\b"
        return len(re.findall(pattern, markdown))

    def _build_chunk_index(
        self,
        *,
        artifact_id: str,
        markdown: str,
        max_chunk_chars: int,
        chunk_overlap_chars: int,
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
        return {
            "sections": sections,
            "chunks": chunks,
            "chunk_count": len(chunks),
            "section_count": len(sections),
        }

    def _build_sections(self, markdown: str) -> list[dict[str, Any]]:
        lines = markdown.splitlines()
        sections: list[dict[str, Any]] = []
        current_title = "Document Start"
        current_level = 0
        current_lines: list[str] = []
        section_index = 0

        def flush() -> None:
            nonlocal section_index, current_lines
            text = "\n".join(current_lines).strip()
            if not text:
                current_lines = []
                return
            section_index += 1
            section_id = self._stable_id(f"section:{section_index}:{current_title}:{text[:120]}")
            sections.append(
                {
                    "section_id": section_id,
                    "index": section_index,
                    "title": current_title,
                    "level": current_level,
                    "text": text,
                }
            )
            current_lines = []

        for line in lines:
            stripped = line.strip()
            heading_match = re.match(r"^(#{1,6})\s+(.+?)\s*$", stripped)
            if heading_match:
                flush()
                current_level = len(heading_match.group(1))
                current_title = heading_match.group(2).strip()
                current_lines = [line]
                continue
            current_lines.append(line)
        flush()

        if not sections and markdown.strip():
            section_id = self._stable_id(f"section:1:{markdown[:120]}")
            sections.append(
                {
                    "section_id": section_id,
                    "index": 1,
                    "title": "Document",
                    "level": 0,
                    "text": markdown.strip(),
                }
            )
        return sections

    def _chunk_section(
        self,
        *,
        artifact_id: str,
        section: dict[str, Any],
        max_chunk_chars: int,
        chunk_overlap_chars: int,
    ) -> list[dict[str, Any]]:
        text = str(section.get("text") or "").strip()
        if not text:
            return []
        chunks: list[dict[str, Any]] = []
        start = 0
        section_text_len = len(text)
        part = 0
        while start < section_text_len:
            end = min(section_text_len, start + max_chunk_chars)
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
                    "text": slice_text,
                    "estimated_chars": len(slice_text),
                }
            )
            if end >= section_text_len:
                break
            start = max(end - chunk_overlap_chars, start + 1)
        return chunks

    def _stable_id(self, raw: str) -> str:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
