"""Segmented access to docs-parser bundles for slide generation.

The slide agent should not rely on the orchestrator to stuff large PDFs into a
single prompt. This module reads local docs.parse_bundle outputs page-by-page or
slide-by-slide so planners/builders can work on bounded windows.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PathResolver = Callable[[str], Path | None]


@dataclass(frozen=True)
class SourceUnit:
    """A single parsed page/slide with nearby text and visual references."""

    doc_index: int
    document_title: str
    filename: str
    unit_kind: str
    unit_number: int
    text: str
    assets: list[dict[str, Any]]

    @property
    def label(self) -> str:
        return f"{self.document_title} {self.unit_kind} {self.unit_number}".strip()


@dataclass(frozen=True)
class _SourceUnitRef:
    retriever_index: int
    unit_index: int


class SlideSourceRetriever:
    """Read windows from one parsed document bundle."""

    def __init__(
        self,
        *,
        doc_index: int,
        document: dict[str, Any],
        manifest_path: Path | None,
        document_md_path: Path | None,
        chunk_index_path: Path | None,
        resolver: PathResolver,
    ) -> None:
        self.doc_index = doc_index
        self.document = document
        self.manifest_path = manifest_path
        self.document_md_path = document_md_path
        self.chunk_index_path = chunk_index_path
        self.resolver = resolver
        self.bundle_root = (manifest_path.parent if manifest_path else None)
        self.markdown = self._read_text(document_md_path)
        self.chunk_index = self._read_json(chunk_index_path)
        self.unit_kind, self.units = self._select_units()
        self.figures = self._entries("figures")
        self.tables = self._entries("tables")
        self.assets = self._entries("assets")

    @classmethod
    def from_document(
        cls,
        *,
        doc_index: int,
        document: dict[str, Any],
        resolver: PathResolver,
    ) -> "SlideSourceRetriever | None":
        paths = document.get("paths") if isinstance(document.get("paths"), dict) else {}
        manifest_path = cls._resolve(paths.get("manifest"), resolver)
        document_md_path = cls._resolve(paths.get("document_md"), resolver)
        chunk_index_path = cls._resolve(paths.get("chunk_index"), resolver)
        if document_md_path is None and chunk_index_path is None:
            return None
        retriever = cls(
            doc_index=doc_index,
            document=document,
            manifest_path=manifest_path,
            document_md_path=document_md_path,
            chunk_index_path=chunk_index_path,
            resolver=resolver,
        )
        return retriever if retriever.units else None

    @property
    def title(self) -> str:
        return (
            self._text(self.document.get("title"))
            or self._text(self.document.get("filename"))
            or f"Document {self.doc_index}"
        )

    @property
    def filename(self) -> str:
        return self._text(self.document.get("filename"))

    def read_unit(self, unit_index: int, *, max_chars: int) -> SourceUnit:
        unit = self.units[unit_index]
        unit_number = self._unit_number(unit, unit_index)
        text = self._unit_text(unit, max_chars=max_chars)
        assets = self._unit_assets(unit, unit_number)
        return SourceUnit(
            doc_index=self.doc_index,
            document_title=self.title,
            filename=self.filename,
            unit_kind=self.unit_kind,
            unit_number=unit_number,
            text=text,
            assets=assets,
        )

    def _select_units(self) -> tuple[str, list[dict[str, Any]]]:
        pages = self._entries("pages")
        slides = self._entries("slides")
        if pages:
            return "page", pages
        if slides:
            return "slide", slides
        chunks = self._entries("chunks")
        if chunks:
            return "section", chunks
        sections = self._entries("sections")
        if sections:
            return "section", sections
        return "page", []

    def _entries(self, key: str) -> list[dict[str, Any]]:
        entries = self.chunk_index.get(key) if isinstance(self.chunk_index.get(key), list) else []
        return [entry for entry in entries if isinstance(entry, dict)]

    def _unit_number(self, unit: dict[str, Any], unit_index: int) -> int:
        for key in ("page_number", "slide_number", "section_number"):
            try:
                value = int(unit.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return unit_index + 1

    def _unit_text(self, unit: dict[str, Any], *, max_chars: int) -> str:
        segment = ""
        try:
            start_char = int(unit.get("start_char"))
            end_char = int(unit.get("end_char"))
        except (TypeError, ValueError):
            start_char = 0
            end_char = 0
        if end_char > start_char and self.markdown:
            segment = self.markdown[start_char:end_char]
        if not segment:
            segment = self._text(
                unit.get("text")
                or unit.get("summary")
                or unit.get("excerpt")
                or unit.get("search_text")
                or unit.get("title")
            )
        return compact_text(segment, limit=max_chars)

    def _unit_assets(self, unit: dict[str, Any], unit_number: int) -> list[dict[str, Any]]:
        assets: list[dict[str, Any]] = []
        seen: set[str] = set()
        candidates = []
        if self._text(unit.get("path")):
            candidates.append({**unit, "kind": unit.get("kind") or f"{self.unit_kind}_image"})
        candidates.extend(self._assets_from_entries(self.assets, unit_number))
        candidates.extend(self._assets_from_entries(self.figures, unit_number))
        candidates.extend(self._assets_from_entries(self.tables, unit_number))
        for raw in candidates:
            asset_id = self._text(raw.get("asset_id") or raw.get("figure_id") or raw.get("table_id") or raw.get("path"))
            if not asset_id or asset_id in seen:
                continue
            seen.add(asset_id)
            item = dict(raw)
            resolved_path = self._resolve_asset_path(item.get("path"))
            if resolved_path is not None:
                item["resolved_path"] = resolved_path.as_posix()
            assets.append(item)
        return assets

    def _assets_from_entries(self, entries: list[dict[str, Any]], unit_number: int) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for entry in entries:
            for key in ("page_number", "slide_number"):
                try:
                    value = int(entry.get(key))
                except (TypeError, ValueError):
                    continue
                if value == unit_number:
                    matches.append(entry)
                    break
        return matches

    def _resolve_asset_path(self, raw_path: Any) -> Path | None:
        value = self._text(raw_path)
        if not value:
            return None
        resolved = self.resolver(value)
        if resolved is not None and resolved.exists():
            return resolved.resolve()
        if self.bundle_root is None:
            return None
        candidate = (self.bundle_root / value).resolve()
        return candidate if candidate.exists() else None

    @staticmethod
    def _resolve(value: Any, resolver: PathResolver) -> Path | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        resolved = resolver(raw)
        if resolved is not None and resolved.exists():
            return resolved.resolve()
        return None

    @staticmethod
    def _read_text(path: Path | None) -> str:
        if path is None or not path.exists() or not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return ""

    @staticmethod
    def _read_json(path: Path | None) -> dict[str, Any]:
        if path is None or not path.exists() or not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _text(value: Any) -> str:
        return str(value or "").strip()


class SlideSourceCollection:
    """Combined retrieval facade across all parsed source documents."""

    def __init__(self, documents: list[dict[str, Any]], resolver: PathResolver) -> None:
        self.retrievers: list[SlideSourceRetriever] = []
        self.refs: list[_SourceUnitRef] = []
        for doc_index, document in enumerate(documents, start=1):
            if not isinstance(document, dict):
                continue
            retriever = SlideSourceRetriever.from_document(
                doc_index=doc_index,
                document=document,
                resolver=resolver,
            )
            if retriever is None:
                continue
            retriever_index = len(self.retrievers)
            self.retrievers.append(retriever)
            for unit_index, _unit in enumerate(retriever.units):
                self.refs.append(_SourceUnitRef(retriever_index=retriever_index, unit_index=unit_index))

    @property
    def unit_count(self) -> int:
        return len(self.refs)

    def window_units(self, start_ordinal: int, end_ordinal: int, *, max_chars_per_unit: int = 1800) -> list[SourceUnit]:
        start = max(1, start_ordinal)
        end = max(start, end_ordinal)
        selected = self.refs[start - 1 : end]
        units: list[SourceUnit] = []
        for ref in selected:
            units.append(
                self.retrievers[ref.retriever_index].read_unit(
                    ref.unit_index,
                    max_chars=max_chars_per_unit,
                )
            )
        return units

    def window_prompt(self, start_ordinal: int, end_ordinal: int, *, max_chars_per_unit: int = 1800) -> str:
        units = self.window_units(start_ordinal, end_ordinal, max_chars_per_unit=max_chars_per_unit)
        if not units:
            return ""
        lines = [
            f"Retrieved source window: source units {start_ordinal}-{end_ordinal} of {self.unit_count}.",
            "Use this window as the detailed source of truth for this deck part.",
        ]
        for unit in units:
            lines.append("")
            lines.append(f"[{unit.document_title} | {unit.unit_kind} {unit.unit_number}]")
            if unit.filename:
                lines.append(f"File: {unit.filename}")
            if unit.text:
                lines.append(unit.text)
            if unit.assets:
                lines.append("Visual references:")
                for asset in unit.assets[:4]:
                    detail = compact_text(
                        asset.get("description")
                        or asset.get("caption")
                        or asset.get("title")
                        or asset.get("kind")
                        or asset.get("asset_id"),
                        limit=220,
                    )
                    path = self._text(asset.get("resolved_path") or asset.get("path"))
                    lines.append("- " + detail + (f" ({path})" if path else ""))
        return "\n".join(lines)

    def one_unit_per_slide_plan(
        self,
        *,
        start_ordinal: int,
        end_ordinal: int,
        deck_title: str,
        deck_theme: str,
    ) -> dict[str, Any]:
        units = self.window_units(start_ordinal, end_ordinal, max_chars_per_unit=1400)
        slides: list[dict[str, Any]] = []
        for index, unit in enumerate(units, start=1):
            title = self._slide_title(unit)
            content_blocks: list[dict[str, Any]] = []
            if unit.text:
                content_blocks.append({"type": "text", "body": compact_text(unit.text, limit=900)})
            asset_description = self._asset_description(unit.assets)
            if asset_description:
                content_blocks.append(
                    {
                        "type": "image_prompt",
                        "description": asset_description,
                        "mood": "professional",
                        "composition": "right-half",
                    }
                )
            if not content_blocks:
                content_blocks.append({"type": "text", "body": f"Source {unit.unit_kind} {unit.unit_number}."})
            slides.append(
                {
                    "slide_number": index,
                    "title": title,
                    "content_role": "visual" if asset_description else "narrative",
                    "layout_reasoning": (
                        f"One slide generated from {unit.document_title} "
                        f"{unit.unit_kind} {unit.unit_number}."
                    ),
                    "full_content": content_blocks,
                    "speaker_notes": (
                        f"This slide summarizes {unit.document_title} "
                        f"{unit.unit_kind} {unit.unit_number} from the source material."
                    ),
                    "source_reference": {
                        "document_title": unit.document_title,
                        "filename": unit.filename,
                        "unit_kind": unit.unit_kind,
                        "unit_number": unit.unit_number,
                    },
                }
            )
        return {
            "input_type_detected": "content",
            "deck_title": deck_title,
            "deck_theme": deck_theme,
            "slides": slides,
        }

    @staticmethod
    def _slide_title(unit: SourceUnit) -> str:
        for raw_line in unit.text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#"):
                return compact_text(line.lstrip("#").strip(), limit=90)
            cleaned = re.sub(r"^\[[^\]]+\]\s*", "", line).strip()
            if cleaned:
                return compact_text(cleaned, limit=90)
        return f"{unit.document_title}: {unit.unit_kind.title()} {unit.unit_number}"

    @staticmethod
    def _asset_description(assets: list[dict[str, Any]]) -> str:
        descriptions: list[str] = []
        for asset in assets[:2]:
            text = compact_text(
                asset.get("description")
                or asset.get("caption")
                or asset.get("title")
                or "",
                limit=220,
            )
            if text:
                descriptions.append(text)
        return " Source visual: ".join(descriptions)

    @staticmethod
    def _text(value: Any) -> str:
        return str(value or "").strip()


def compact_text(value: Any, *, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."
