"""Google Sheets structure helpers and conservative table parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_A1_WITH_SHEET_RE = re.compile(r"^'?(?P<sheet>[^'!]+)'?!\s*(?P<cells>[A-Za-z]+[0-9]+(?::[A-Za-z]+[0-9]+)?)$")
_A1_CELLS_RE = re.compile(r"^[A-Za-z]+[0-9]+(?::[A-Za-z]+[0-9]+)?$")


@dataclass(slots=True)
class SheetInfo:
    sheet_id: int | None
    title: str
    row_count: int
    column_count: int
    frozen_row_count: int = 0
    frozen_column_count: int = 0


class SheetNavigator:
    """Live workbook map used by the planner and deterministic executor."""

    def __init__(self, spreadsheet: dict[str, Any]) -> None:
        self.spreadsheet_id = str(spreadsheet.get("spreadsheet_id") or "").strip()
        self.title = str(spreadsheet.get("title") or "").strip()
        raw_sheets = spreadsheet.get("sheets") if isinstance(spreadsheet.get("sheets"), list) else []
        self.sheets = [
            SheetInfo(
                sheet_id=item.get("sheet_id"),
                title=str(item.get("title") or "").strip(),
                row_count=int(item.get("row_count") or 0),
                column_count=int(item.get("column_count") or 0),
                frozen_row_count=int(item.get("frozen_row_count") or 0),
                frozen_column_count=int(item.get("frozen_column_count") or 0),
            )
            for item in raw_sheets
            if str(item.get("title") or "").strip()
        ]
        self.active_sheet = self.sheets[0].title if self.sheets else "Sheet1"
        self._by_title = {item.title.lower(): item for item in self.sheets}

    def sheet_id_for(self, sheet_name: str | None = None) -> int | None:
        sheet = self.sheet_for(sheet_name)
        return sheet.sheet_id if sheet else None

    def sheet_for(self, sheet_name: str | None = None) -> SheetInfo | None:
        name = str(sheet_name or self.active_sheet or "").strip()
        if not name:
            return self.sheets[0] if self.sheets else None
        return self._by_title.get(name.lower())

    def ensure_range(self, range_name: str, *, default_sheet: str | None = None) -> str:
        raw = str(range_name or "").strip()
        if not raw:
            raise ValueError("range is required.")
        if "!" in raw:
            match = _A1_WITH_SHEET_RE.match(raw)
            if not match:
                raise ValueError(f"Unsupported A1 range: {range_name!r}")
            sheet_name = match.group("sheet").strip("' ")
            if self.sheet_for(sheet_name) is None:
                raise ValueError(f"Sheet tab not found: {sheet_name}")
            cells = match.group("cells").replace(" ", "")
            return f"{quote_sheet_name(sheet_name)}!{cells}"
        if not _A1_CELLS_RE.match(raw):
            raise ValueError(f"Unsupported A1 range: {range_name!r}")
        sheet_name = str(default_sheet or self.active_sheet or "Sheet1").strip()
        if self.sheet_for(sheet_name) is None:
            raise ValueError(f"Sheet tab not found: {sheet_name}")
        return f"{quote_sheet_name(sheet_name)}!{raw}"

    def summary(self) -> dict[str, Any]:
        return {
            "spreadsheet_id": self.spreadsheet_id,
            "title": self.title,
            "active_sheet": self.active_sheet,
            "sheets": [
                {
                    "sheet_id": item.sheet_id,
                    "title": item.title,
                    "row_count": item.row_count,
                    "column_count": item.column_count,
                    "frozen_row_count": item.frozen_row_count,
                    "frozen_column_count": item.frozen_column_count,
                }
                for item in self.sheets
            ],
        }


def quote_sheet_name(sheet_name: str) -> str:
    cleaned = str(sheet_name or "Sheet1").replace("'", "''").strip() or "Sheet1"
    return f"'{cleaned}'"


def parse_markdown_tables(markdown_text: str) -> list[dict[str, Any]]:
    """Parse simple pipe tables into row arrays.

    This is deliberately conservative. It captures table-shaped blocks so the
    Sheets executor can create real cells instead of dumping markdown text.
    """

    tables: list[dict[str, Any]] = []
    lines = [line.rstrip() for line in str(markdown_text or "").splitlines()]
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not _looks_like_table_row(line) or index + 1 >= len(lines):
            index += 1
            continue
        separator = lines[index + 1].strip()
        if not _looks_like_separator(separator):
            index += 1
            continue
        rows = [_split_pipe_row(line)]
        index += 2
        while index < len(lines) and _looks_like_table_row(lines[index].strip()):
            rows.append(_split_pipe_row(lines[index].strip()))
            index += 1
        if rows and rows[0]:
            width = max(len(row) for row in rows)
            normalized_rows = [row + [""] * (width - len(row)) for row in rows]
            tables.append({"values": normalized_rows, "has_header": True})
        continue
    return tables


def rows_from_input(input_data: dict[str, Any]) -> list[list[Any]]:
    for key in ("values", "rows", "data"):
        candidate = input_data.get(key)
        rows = normalize_rows(candidate)
        if rows:
            return rows
    text = str(input_data.get("body_markdown") or input_data.get("content") or "").strip()
    tables = parse_markdown_tables(text)
    if tables:
        return normalize_rows(tables[0].get("values"))
    return []


def normalize_rows(value: Any) -> list[list[Any]]:
    if not isinstance(value, list) or not value:
        return []
    rows: list[list[Any]] = []
    for item in value:
        if isinstance(item, dict):
            if not rows:
                rows.append(list(item.keys()))
            rows.append([item.get(key, "") for key in rows[0]])
        elif isinstance(item, list):
            rows.append([_cell_value(cell) for cell in item])
        else:
            rows.append([_cell_value(item)])
    return rows


def count_cells(rows: list[list[Any]]) -> int:
    return sum(len(row) for row in rows)


def values_preview(values: list[list[Any]], *, max_rows: int = 10, max_cols: int = 10) -> list[list[Any]]:
    return [row[:max_cols] for row in values[:max_rows]]


def _cell_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _looks_like_table_row(line: str) -> bool:
    return line.startswith("|") and line.endswith("|") and line.count("|") >= 2


def _looks_like_separator(line: str) -> bool:
    if not _looks_like_table_row(line):
        return False
    cells = [cell.strip() for cell in line.strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", cell or "") for cell in cells)


def _split_pipe_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]

