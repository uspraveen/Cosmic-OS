"""Google Docs structure helpers and Markdown-to-Docs request builder.

The Markdown parser and block-map design are ported from the standalone
`workspace_agent_core.py` Google Docs agent, but made stateless for COSMIC's
TaskEnvelope specialist runtime.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any


class MarkdownParser:
    """Convert a practical Markdown subset into Google Docs batchUpdate requests."""

    @staticmethod
    def _find_balanced_end(text: str, marker: str, start_idx: int) -> int:
        return text.find(marker, start_idx)

    @staticmethod
    def _style_fields(style: dict[str, Any]) -> str:
        preferred_order = [
            "bold",
            "italic",
            "underline",
            "strikethrough",
            "weightedFontFamily",
            "foregroundColor",
            "backgroundColor",
            "link",
        ]
        fields: list[str] = []
        for key in preferred_order:
            if key in style:
                fields.append(key)
        for key in sorted(style.keys()):
            if key not in fields:
                fields.append(key)
        return ",".join(fields)

    @staticmethod
    def _parse_color(color_str: str) -> tuple[float, float, float] | None:
        c = color_str.strip().lower()
        named: dict[str, tuple[float, float, float]] = {
            "red": (1.00, 0.20, 0.20),
            "green": (0.13, 0.55, 0.13),
            "blue": (0.10, 0.35, 0.90),
            "yellow": (1.00, 0.95, 0.00),
            "orange": (1.00, 0.55, 0.00),
            "purple": (0.50, 0.10, 0.70),
            "pink": (1.00, 0.45, 0.65),
            "cyan": (0.00, 0.80, 0.85),
            "teal": (0.00, 0.55, 0.55),
            "brown": (0.55, 0.27, 0.07),
            "white": (1.00, 1.00, 1.00),
            "black": (0.00, 0.00, 0.00),
            "gray": (0.50, 0.50, 0.50),
            "grey": (0.50, 0.50, 0.50),
            "darkred": (0.55, 0.00, 0.00),
            "darkgreen": (0.00, 0.39, 0.00),
            "darkblue": (0.00, 0.00, 0.55),
            "lightgray": (0.83, 0.83, 0.83),
            "lightgrey": (0.83, 0.83, 0.83),
        }
        if c in named:
            return named[c]
        if c.startswith("#") and len(c) == 7:
            try:
                return (
                    int(c[1:3], 16) / 255.0,
                    int(c[3:5], 16) / 255.0,
                    int(c[5:7], 16) / 255.0,
                )
            except ValueError:
                return None
        return None

    @classmethod
    def _parse_inline_styles(cls, text: str) -> tuple[str, list[dict[str, Any]]]:
        if not text:
            return "", []

        def shift_spans(spans: list[dict[str, Any]], offset: int) -> list[dict[str, Any]]:
            return [
                {
                    "start": span["start"] + offset,
                    "end": span["end"] + offset,
                    "style": span["style"],
                    "fields": span["fields"],
                }
                for span in spans
            ]

        def parse_segment(segment: str) -> tuple[str, list[dict[str, Any]]]:
            plain_parts: list[str] = []
            spans: list[dict[str, Any]] = []
            i = 0
            out_pos = 0
            n = len(segment)

            while i < n:
                if segment[i] == "[":
                    close_bracket = segment.find("]", i + 1)
                    if close_bracket != -1 and close_bracket + 1 < n and segment[close_bracket + 1] == "(":
                        close_paren = segment.find(")", close_bracket + 2)
                        if close_paren != -1:
                            label = segment[i + 1 : close_bracket]
                            url = segment[close_bracket + 2 : close_paren].strip()
                            if label and (url.startswith("http://") or url.startswith("https://")):
                                label_plain, label_spans = parse_segment(label)
                                start = out_pos
                                plain_parts.append(label_plain)
                                out_pos += len(label_plain)
                                if label_plain:
                                    spans.extend(shift_spans(label_spans, start))
                                    link_style = {"link": {"url": url}}
                                    spans.append(
                                        {
                                            "start": start,
                                            "end": out_pos,
                                            "style": link_style,
                                            "fields": cls._style_fields(link_style),
                                        }
                                    )
                                i = close_paren + 1
                                continue

                if segment[i] == "`":
                    end = segment.find("`", i + 1)
                    if end != -1:
                        inner = segment[i + 1 : end]
                        start = out_pos
                        plain_parts.append(inner)
                        out_pos += len(inner)
                        if inner:
                            code_style = {"weightedFontFamily": {"fontFamily": "Courier New"}}
                            spans.append(
                                {
                                    "start": start,
                                    "end": out_pos,
                                    "style": code_style,
                                    "fields": cls._style_fields(code_style),
                                }
                            )
                        i = end + 1
                        continue

                if segment.startswith("<u>", i):
                    end_tag = segment.find("</u>", i + 3)
                    if end_tag != -1:
                        inner_plain, inner_spans = parse_segment(segment[i + 3 : end_tag])
                        start = out_pos
                        plain_parts.append(inner_plain)
                        out_pos += len(inner_plain)
                        if inner_plain:
                            spans.extend(shift_spans(inner_spans, start))
                            underline_style = {"underline": True}
                            spans.append(
                                {
                                    "start": start,
                                    "end": out_pos,
                                    "style": underline_style,
                                    "fields": cls._style_fields(underline_style),
                                }
                            )
                        i = end_tag + 4
                        continue

                if segment.startswith("<color:", i):
                    close_gt = segment.find(">", i + 7)
                    if close_gt != -1:
                        color_val = segment[i + 7 : close_gt].strip()
                        end_tag = segment.find("</color>", close_gt + 1)
                        rgb = cls._parse_color(color_val)
                        if end_tag != -1 and rgb:
                            inner_plain, inner_spans = parse_segment(segment[close_gt + 1 : end_tag])
                            start = out_pos
                            plain_parts.append(inner_plain)
                            out_pos += len(inner_plain)
                            if inner_plain:
                                spans.extend(shift_spans(inner_spans, start))
                                color_style = {
                                    "foregroundColor": {
                                        "color": {
                                            "rgbColor": {
                                                "red": rgb[0],
                                                "green": rgb[1],
                                                "blue": rgb[2],
                                            }
                                        }
                                    }
                                }
                                spans.append(
                                    {
                                        "start": start,
                                        "end": out_pos,
                                        "style": color_style,
                                        "fields": cls._style_fields(color_style),
                                    }
                                )
                            i = end_tag + 8
                            continue

                if segment.startswith("<highlight:", i):
                    close_gt = segment.find(">", i + 11)
                    if close_gt != -1:
                        color_val = segment[i + 11 : close_gt].strip()
                        end_tag = segment.find("</highlight>", close_gt + 1)
                        rgb = cls._parse_color(color_val)
                        if end_tag != -1 and rgb:
                            inner_plain, inner_spans = parse_segment(segment[close_gt + 1 : end_tag])
                            start = out_pos
                            plain_parts.append(inner_plain)
                            out_pos += len(inner_plain)
                            if inner_plain:
                                spans.extend(shift_spans(inner_spans, start))
                                hl_style = {
                                    "backgroundColor": {
                                        "color": {
                                            "rgbColor": {
                                                "red": rgb[0],
                                                "green": rgb[1],
                                                "blue": rgb[2],
                                            }
                                        }
                                    }
                                }
                                spans.append(
                                    {
                                        "start": start,
                                        "end": out_pos,
                                        "style": hl_style,
                                        "fields": cls._style_fields(hl_style),
                                    }
                                )
                            i = end_tag + 12
                            continue

                matched = False
                for marker, style in (
                    ("***", {"bold": True, "italic": True}),
                    ("**", {"bold": True}),
                    ("__", {"underline": True}),
                    ("*", {"italic": True}),
                    ("_", {"italic": True}),
                ):
                    if segment.startswith(marker, i):
                        end = cls._find_balanced_end(segment, marker, i + len(marker))
                        if end != -1:
                            inner_plain, inner_spans = parse_segment(segment[i + len(marker) : end])
                            start = out_pos
                            plain_parts.append(inner_plain)
                            out_pos += len(inner_plain)
                            if inner_plain:
                                spans.extend(shift_spans(inner_spans, start))
                                spans.append(
                                    {
                                        "start": start,
                                        "end": out_pos,
                                        "style": style,
                                        "fields": cls._style_fields(style),
                                    }
                                )
                            i = end + len(marker)
                            matched = True
                            break
                if matched:
                    continue

                plain_parts.append(segment[i])
                out_pos += 1
                i += 1

            return "".join(plain_parts), spans

        return parse_segment(text)

    @classmethod
    def parse(cls, full_text: str, start_index: int = 1) -> list[dict[str, Any]]:
        requests: list[dict[str, Any]] = []
        current_index = start_index
        lines = full_text.split("\n")
        in_code_block = False

        for line in lines:
            fence_match = re.match(r"^\s*```([A-Za-z0-9_+\-]*)?\s*$", line)
            if fence_match:
                in_code_block = not in_code_block
                continue

            line_stripped = line.strip()
            para_style = "NORMAL_TEXT"
            list_preset: str | None = None
            quote_style = False
            code_line = False

            if in_code_block:
                content_plain = line.rstrip("\r")
                inline_spans: list[dict[str, Any]] = []
                code_line = True
            else:
                heading_match = re.match(r"^\s*(#{1,6})\s+(.*)$", line)
                ordered_match = re.match(r"^\s*\d+\.\s+(.*)$", line)
                checkbox_match = re.match(r"^\s*[-*]\s+\[(?: |x|X)\]\s+(.*)$", line)

                if heading_match:
                    level = len(heading_match.group(1))
                    para_style = f"HEADING_{min(level, 6)}"
                    content_raw = heading_match.group(2)
                elif line_stripped.startswith("> "):
                    content_raw = line_stripped[2:]
                    quote_style = True
                elif checkbox_match:
                    list_preset, content_raw = "BULLET_CHECKBOX", checkbox_match.group(1)
                elif line_stripped.startswith("* ") or line_stripped.startswith("- "):
                    list_preset, content_raw = "BULLET_DISC_CIRCLE_SQUARE", line_stripped[2:]
                elif ordered_match:
                    list_preset, content_raw = "NUMBERED_DECIMAL_ALPHA_ROMAN", ordered_match.group(1)
                else:
                    content_raw = line

                content_plain, inline_spans = cls._parse_inline_styles(content_raw)

            full_insertion = content_plain + "\n"
            requests.append({"insertText": {"location": {"index": current_index}, "text": full_insertion}})
            para_end_index = current_index + len(full_insertion)

            if para_style != "NORMAL_TEXT":
                requests.append(
                    {
                        "updateParagraphStyle": {
                            "range": {"startIndex": current_index, "endIndex": para_end_index - 1},
                            "paragraphStyle": {"namedStyleType": para_style},
                            "fields": "namedStyleType",
                        }
                    }
                )
            elif quote_style:
                requests.append(
                    {
                        "updateParagraphStyle": {
                            "range": {"startIndex": current_index, "endIndex": para_end_index - 1},
                            "paragraphStyle": {"indentStart": {"magnitude": 18, "unit": "PT"}},
                            "fields": "indentStart",
                        }
                    }
                )

            if list_preset:
                requests.append(
                    {
                        "createParagraphBullets": {
                            "range": {"startIndex": current_index, "endIndex": para_end_index - 1},
                            "bulletPreset": list_preset,
                        }
                    }
                )

            if code_line and content_plain:
                code_style = {"weightedFontFamily": {"fontFamily": "Courier New"}}
                requests.append(
                    {
                        "updateTextStyle": {
                            "range": {
                                "startIndex": current_index,
                                "endIndex": current_index + len(content_plain),
                            },
                            "textStyle": code_style,
                            "fields": cls._style_fields(code_style),
                        }
                    }
                )
            else:
                for span in inline_spans:
                    start = current_index + span["start"]
                    end = current_index + span["end"]
                    if end > start:
                        requests.append(
                            {
                                "updateTextStyle": {
                                    "range": {"startIndex": start, "endIndex": end},
                                    "textStyle": span["style"],
                                    "fields": span["fields"],
                                }
                            }
                        )

            current_index = para_end_index
        return requests


@dataclass(slots=True)
class BlockMap:
    blocks: list[dict[str, Any]]
    tables: list[dict[str, Any]]
    images: list[dict[str, Any]]

    def get_block(self, block_id: str) -> dict[str, Any] | None:
        for block in self.blocks:
            if block.get("id") == block_id:
                return block
        return None

    def get_block_by_content(self, content_snippet: str) -> dict[str, Any] | None:
        snippet = str(content_snippet or "").strip().lower()
        if not snippet:
            return None
        for block in self.blocks:
            if snippet in str(block.get("text") or "").strip().lower():
                return block
        return None

    def outline(self, *, max_blocks: int = 120) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for block in self.blocks[:max_blocks]:
            text = str(block.get("text") or "").strip()
            style = str(block.get("style") or "")
            if not text:
                continue
            result.append(
                {
                    "block_id": block.get("id"),
                    "style": style,
                    "text": text[:240],
                    "is_heading": "HEADING" in style,
                    "start_index": block.get("start"),
                    "end_index": block.get("end"),
                }
            )
        return result

    def full_text(self, *, max_chars: int = 30000) -> str:
        parts = []
        remaining = max_chars
        for block in self.blocks:
            text = str(block.get("text") or "").strip()
            if not text:
                continue
            line = f"[{block.get('id')}] {text}"
            if len(line) > remaining:
                parts.append(line[:remaining])
                break
            parts.append(line)
            remaining -= len(line) + 1
            if remaining <= 0:
                break
        return "\n".join(parts)


def build_block_map(document: dict[str, Any]) -> BlockMap:
    body_content = ((document.get("body") or {}).get("content") or [])
    blocks: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    content_counts: dict[str, int] = {}

    def stable_id(text: str, style: str, sequence: int) -> str:
        normalized = text.strip()[:50].lower()
        raw = f"{normalized}-{style}"
        base_id = f"blk_{hashlib.md5(raw.encode('utf-8')).hexdigest()[:8]}"
        return f"{base_id}_{sequence}" if sequence > 0 else base_id

    for index, element in enumerate(body_content):
        start = element.get("startIndex")
        end = element.get("endIndex")
        if "paragraph" in element:
            paragraph = element.get("paragraph") or {}
            elements = paragraph.get("elements") or []
            text_run = "".join(
                str(item.get("textRun", {}).get("content", ""))
                for item in elements
            )
            style = str((paragraph.get("paragraphStyle") or {}).get("namedStyleType") or "NORMAL_TEXT")
            if isinstance(start, int) and isinstance(end, int) and end > start:
                content_key = f"{text_run.strip()[:50]}-{style}"
                sequence = content_counts.get(content_key, 0)
                content_counts[content_key] = sequence + 1
                block_id = stable_id(text_run, style, sequence)
                block = {
                    "id": block_id,
                    "index": index,
                    "text": text_run,
                    "style": style,
                    "start": start,
                    "end": end,
                    "content_hash": hashlib.md5(text_run.encode("utf-8")).hexdigest()[:10],
                }
                blocks.append(block)
                for item in elements:
                    inline = item.get("inlineObjectElement")
                    if inline:
                        images.append(
                            {
                                "object_id": inline.get("inlineObjectId"),
                                "block_id": block_id,
                                "start": item.get("startIndex"),
                                "end": item.get("endIndex"),
                                "context": text_run.strip()[:160],
                                "type": "inline",
                            }
                        )
        elif "table" in element:
            table = element.get("table") or {}
            rows = []
            for row in table.get("tableRows") or []:
                cells = []
                for cell in row.get("tableCells") or []:
                    cell_text_parts: list[str] = []
                    for cell_element in cell.get("content") or []:
                        if "paragraph" not in cell_element:
                            continue
                        for item in (cell_element.get("paragraph") or {}).get("elements") or []:
                            cell_text_parts.append(str(item.get("textRun", {}).get("content", "")))
                    cells.append("".join(cell_text_parts).strip())
                rows.append(cells)
            table_id = f"tbl_{hashlib.md5(str(rows).encode('utf-8')).hexdigest()[:8]}"
            tables.append(
                {
                    "id": table_id,
                    "index": index,
                    "start": start,
                    "end": end,
                    "rows": rows,
                    "row_count": len(rows),
                    "column_count": max((len(row) for row in rows), default=0),
                }
            )

    inline_objects = document.get("inlineObjects") or {}
    for image in images:
        object_id = str(image.get("object_id") or "")
        inline_object = inline_objects.get(object_id) if object_id else None
        embedded = (((inline_object or {}).get("inlineObjectProperties") or {}).get("embeddedObject") or {})
        image_props = embedded.get("imageProperties") or {}
        image["source_uri"] = image_props.get("sourceUri") or image_props.get("contentUri") or ""
        image["size"] = embedded.get("size") or {}

    return BlockMap(blocks=blocks, tables=tables, images=images)


def document_summary(document: dict[str, Any], *, max_read_chars: int = 30000, max_blocks: int = 200) -> dict[str, Any]:
    block_map = build_block_map(document)
    revision_id = str(document.get("revisionId") or "")
    title = str(document.get("title") or "")
    return {
        "document_id": str(document.get("documentId") or ""),
        "title": title,
        "revision_id": revision_id,
        "outline": block_map.outline(max_blocks=max_blocks),
        "full_text": block_map.full_text(max_chars=max_read_chars),
        "blocks": block_map.blocks[:max_blocks],
        "tables": block_map.tables,
        "images": block_map.images,
        "block_count": len(block_map.blocks),
        "table_count": len(block_map.tables),
        "image_count": len(block_map.images),
    }


def markdown_probe_text(markdown_text: str, *, max_len: int = 80) -> str:
    lines = []
    in_code = False
    for line in str(markdown_text or "").splitlines():
        if re.match(r"^\s*```", line):
            in_code = not in_code
            continue
        if in_code:
            plain = line.strip()
        else:
            plain, _spans = MarkdownParser._parse_inline_styles(re.sub(r"^\s*#{1,6}\s+", "", line))
            plain = re.sub(r"^\s*[-*]\s+|\s*\d+\.\s+", "", plain).strip()
        if plain:
            lines.append(plain)
        if sum(len(item) for item in lines) >= max_len:
            break
    return " ".join(lines)[:max_len].strip()

