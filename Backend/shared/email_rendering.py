from __future__ import annotations

from dataclasses import dataclass
import html
import re


@dataclass(frozen=True)
class RenderedEmailBody:
    text_body: str
    html_body: str


def render_markdown_email_bodies(markdown_text: str) -> RenderedEmailBody:
    """Render assistant Markdown into multipart email bodies.

    The text part is readable in plain-text clients. The HTML part preserves
    headings, lists, links, emphasis, code blocks, and Markdown tables for
    clients like Gmail.
    """

    return RenderedEmailBody(
        text_body=render_markdown_email_text(markdown_text),
        html_body=render_markdown_email_html(markdown_text),
    )


def render_markdown_email_text(markdown_text: str) -> str:
    normalized = str(markdown_text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"```[a-zA-Z0-9_+-]+\n", "```\n", normalized)
    normalized = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1: \2", normalized)
    normalized = _render_markdown_tables_as_text(normalized)

    lines: list[str] = []
    in_code_block = False
    for raw_line in normalized.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            lines.append(line)
            continue
        if not stripped:
            lines.append("")
            continue
        if re.fullmatch(r"-{3,}|_{3,}|\*{3,}", stripped):
            lines.append("────────")
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.+?)\s*$", stripped)
        if heading_match:
            lines.append(heading_match.group(2).strip())
            continue

        bullet_match = re.match(r"^[-*+]\s+(.+)$", stripped)
        if bullet_match:
            lines.append(f"• {bullet_match.group(1).strip()}")
            continue

        ordered_match = re.match(r"^(\d+)\.\s+(.+)$", stripped)
        if ordered_match:
            lines.append(f"{ordered_match.group(1)}. {ordered_match.group(2).strip()}")
            continue

        if stripped.startswith(">"):
            lines.append(stripped.lstrip(">").strip())
            continue

        lines.append(stripped)

    plain = "\n".join(lines)
    plain = re.sub(r"`([^`]+)`", r"\1", plain)
    plain = re.sub(r"\*\*(.+?)\*\*", r"\1", plain)
    plain = re.sub(r"__(.+?)__", r"\1", plain)
    plain = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", plain)
    plain = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"\1", plain)
    plain = re.sub(r"\n{3,}", "\n\n", plain)
    return plain.strip() or "[empty response]"


def render_markdown_email_html(markdown_text: str) -> str:
    normalized = str(markdown_text or "").replace("\r\n", "\n").replace("\r", "\n")
    blocks = _render_html_blocks(normalized)
    body = "\n".join(blocks).strip() or "<p>(empty response)</p>"
    return f"<div>{body}</div>"


def _render_html_blocks(markdown_text: str) -> list[str]:
    lines = markdown_text.splitlines()
    blocks: list[str] = []
    index = 0

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            index += 1
            continue

        if stripped.startswith("```"):
            code_lines: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code_lines.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            code_html = html.escape("\n".join(code_lines).strip("\n"))
            blocks.append(f"<pre><code>{code_html}</code></pre>")
            continue

        if index + 1 < len(lines) and _looks_like_table_row(line) and _is_markdown_table_separator(lines[index + 1]):
            table_lines = [line, lines[index + 1]]
            index += 2
            while index < len(lines) and _looks_like_table_row(lines[index]):
                table_lines.append(lines[index])
                index += 1
            blocks.append(_format_markdown_table_html(table_lines))
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.+?)\s*$", stripped)
        if heading_match:
            level = min(len(heading_match.group(1)), 6)
            blocks.append(f"<h{level}>{_render_inline_html(heading_match.group(2).strip())}</h{level}>")
            index += 1
            continue

        if re.fullmatch(r"-{3,}|_{3,}|\*{3,}", stripped):
            blocks.append("<hr/>")
            index += 1
            continue

        bullet_match = re.match(r"^[-*+]\s+(.+)$", stripped)
        ordered_match = re.match(r"^(\d+)\.\s+(.+)$", stripped)
        if bullet_match or ordered_match:
            ordered = ordered_match is not None
            tag = "ol" if ordered else "ul"
            items: list[str] = []
            while index < len(lines):
                candidate = lines[index].strip()
                next_bullet = re.match(r"^[-*+]\s+(.+)$", candidate)
                next_ordered = re.match(r"^(\d+)\.\s+(.+)$", candidate)
                if ordered:
                    if not next_ordered:
                        break
                    items.append(f"<li>{_render_inline_html(next_ordered.group(2).strip())}</li>")
                else:
                    if not next_bullet:
                        break
                    items.append(f"<li>{_render_inline_html(next_bullet.group(1).strip())}</li>")
                index += 1
            blocks.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue

        if stripped.startswith(">"):
            quote_lines: list[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quote_lines.append(lines[index].strip().lstrip(">").strip())
                index += 1
            quote_html = "<br/>".join(_render_inline_html(item) for item in quote_lines if item)
            blocks.append(f"<blockquote>{quote_html}</blockquote>")
            continue

        paragraph_lines: list[str] = []
        while index < len(lines):
            candidate = lines[index]
            candidate_stripped = candidate.strip()
            if not candidate_stripped:
                break
            if candidate_stripped.startswith("```"):
                break
            if re.match(r"^(#{1,6})\s+", candidate_stripped):
                break
            if re.fullmatch(r"-{3,}|_{3,}|\*{3,}", candidate_stripped):
                break
            if re.match(r"^[-*+]\s+(.+)$", candidate_stripped):
                break
            if re.match(r"^(\d+)\.\s+(.+)$", candidate_stripped):
                break
            if candidate_stripped.startswith(">"):
                break
            if (
                index + 1 < len(lines)
                and _looks_like_table_row(candidate)
                and _is_markdown_table_separator(lines[index + 1])
            ):
                break
            paragraph_lines.append(candidate_stripped)
            index += 1
        blocks.append(f"<p>{'<br/>'.join(_render_inline_html(item) for item in paragraph_lines)}</p>")

    return blocks


def _render_inline_html(text: str) -> str:
    rendered = html.escape(str(text or ""))
    rendered = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>',
        rendered,
    )
    rendered = re.sub(r"`([^`]+)`", r"<code>\1</code>", rendered)
    rendered = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", rendered)
    rendered = re.sub(r"__(.+?)__", r"<strong>\1</strong>", rendered)
    rendered = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", rendered)
    rendered = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"<em>\1</em>", rendered)
    return rendered


def _render_markdown_tables_as_text(text: str) -> str:
    lines = text.splitlines()
    rendered: list[str] = []
    index = 0
    in_fenced_block = False

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fenced_block = not in_fenced_block
            rendered.append(line)
            index += 1
            continue

        if (
            not in_fenced_block
            and index + 1 < len(lines)
            and _looks_like_table_row(line)
            and _is_markdown_table_separator(lines[index + 1])
        ):
            table_lines = [line, lines[index + 1]]
            index += 2
            while index < len(lines) and _looks_like_table_row(lines[index]):
                table_lines.append(lines[index])
                index += 1
            rendered.append(_format_markdown_table_text(table_lines))
            continue

        rendered.append(line)
        index += 1

    return "\n".join(rendered)


def _format_markdown_table_text(table_lines: list[str]) -> str:
    rows = [_parse_markdown_table_row(line) for line in table_lines]
    if len(rows) < 2 or not rows[0]:
        return "\n".join(table_lines)

    header = rows[0]
    data_rows = [row for row in rows[2:] if any(cell for cell in row)]
    column_count = max(len(header), *(len(row) for row in data_rows)) if data_rows else len(header)
    if column_count == 0:
        return "\n".join(table_lines)

    normalized_header = _normalize_table_cells(header, column_count)
    normalized_rows = [_normalize_table_cells(row, column_count) for row in data_rows]

    rendered_lines: list[str] = []
    for row in normalized_rows:
        parts: list[str] = []
        for column_index, cell in enumerate(row):
            if not cell:
                continue
            column_name = normalized_header[column_index] or f"Column {column_index + 1}"
            parts.append(f"{column_name}: {cell}")
        if parts:
            rendered_lines.append("• " + " | ".join(parts))
    return "\n".join(rendered_lines) if rendered_lines else "\n".join(table_lines)


def _format_markdown_table_html(table_lines: list[str]) -> str:
    rows = [_parse_markdown_table_row(line) for line in table_lines]
    if len(rows) < 2 or not rows[0]:
        return f"<p>{_render_inline_html(' '.join(line.strip() for line in table_lines if line.strip()))}</p>"

    header = rows[0]
    data_rows = [row for row in rows[2:] if any(cell for cell in row)]
    column_count = max(len(header), *(len(row) for row in data_rows)) if data_rows else len(header)
    normalized_header = _normalize_table_cells(header, column_count)
    normalized_rows = [_normalize_table_cells(row, column_count) for row in data_rows]

    table_style = (
        "border-collapse:collapse;width:100%;margin:12px 0;"
        "font-family:Arial,sans-serif;font-size:14px;line-height:1.4;"
    )
    header_style = (
        "border:1px solid #d0d7de;background:#f6f8fa;color:#111827;"
        "padding:8px 10px;text-align:left;vertical-align:top;font-weight:700;"
    )
    cell_style = (
        "border:1px solid #d0d7de;color:#111827;padding:8px 10px;"
        "text-align:left;vertical-align:top;"
    )
    thead_cells = "".join(f'<th style="{header_style}">{_render_inline_html(cell)}</th>' for cell in normalized_header)
    body_rows = []
    for row in normalized_rows:
        cells = "".join(f'<td style="{cell_style}">{_render_inline_html(cell)}</td>' for cell in row)
        body_rows.append(f"<tr>{cells}</tr>")
    tbody = "".join(body_rows)
    return f'<table style="{table_style}"><thead><tr>{thead_cells}</tr></thead><tbody>{tbody}</tbody></table>'


def _normalize_table_cells(row: list[str], column_count: int) -> list[str]:
    cells = [cell.strip() for cell in row[:column_count]]
    if len(cells) < column_count:
        cells.extend([""] * (column_count - len(cells)))
    return cells


def _looks_like_table_row(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and "|" in stripped


def _is_markdown_table_separator(line: str) -> bool:
    stripped = line.strip()
    return bool(re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?", stripped))


def _parse_markdown_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]
