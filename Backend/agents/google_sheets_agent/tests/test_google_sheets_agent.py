"""Focused tests for the Google Sheets specialist agent."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BACKEND_ROOT))

from agents.google_sheets_agent.agent import GoogleSheetsAgent
from agents.google_sheets_agent.google_sheets_client import GoogleSheetsClient, normalize_spreadsheet
from agents.google_sheets_agent.sheet_structure import (
    SheetNavigator,
    count_cells,
    parse_markdown_tables,
    rows_from_input,
)


def _sample_spreadsheet() -> dict:
    return normalize_spreadsheet(
        {
            "spreadsheetId": "sheet_123",
            "properties": {
                "title": "Copper Tracker",
                "locale": "en_US",
                "timeZone": "America/Chicago",
            },
            "sheets": [
                {
                    "properties": {
                        "sheetId": 111,
                        "title": "Pipeline",
                        "index": 0,
                        "gridProperties": {
                            "rowCount": 100,
                            "columnCount": 12,
                            "frozenRowCount": 1,
                        },
                    }
                }
            ],
        }
    )


def test_intent_schemas_are_valid_json() -> None:
    schema_dir = Path(__file__).resolve().parents[1] / "schemas" / "intents"
    files = sorted(schema_dir.glob("*.json"))
    assert files
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["type"] == "object"


def test_sheet_navigator_adds_active_sheet_to_a1_range() -> None:
    navigator = SheetNavigator(_sample_spreadsheet())
    assert navigator.active_sheet == "Pipeline"
    assert navigator.ensure_range("A1:C3") == "'Pipeline'!A1:C3"
    assert navigator.ensure_range("A2:B4", default_sheet="Pipeline") == "'Pipeline'!A2:B4"
    assert navigator.grid_range("B2:D5") == {
        "sheetId": 111,
        "startRowIndex": 1,
        "endRowIndex": 5,
        "startColumnIndex": 1,
        "endColumnIndex": 4,
    }


def test_sheet_navigator_rejects_unknown_sheet() -> None:
    navigator = SheetNavigator(_sample_spreadsheet())
    with pytest.raises(ValueError, match="Sheet tab not found"):
        navigator.ensure_range("'Unknown'!A1:B2")


def test_markdown_pipe_table_becomes_cell_rows() -> None:
    markdown = """
| Name | Email | Status |
|---|---|---|
| Prof Nick | n@example.com | Intro requested |
| Eduardo | e@example.com | To contact |
"""
    tables = parse_markdown_tables(markdown)
    assert tables == [
        {
            "values": [
                ["Name", "Email", "Status"],
                ["Prof Nick", "n@example.com", "Intro requested"],
                ["Eduardo", "e@example.com", "To contact"],
            ],
            "has_header": True,
        }
    ]
    assert rows_from_input({"body_markdown": markdown})[0] == ["Name", "Email", "Status"]


def test_count_cells_handles_rectangular_and_short_rows() -> None:
    assert count_cells([["a", "b"], ["c"]]) == 3


def test_google_api_error_detail_preserves_message() -> None:
    request = httpx.Request("POST", "https://sheets.googleapis.com/v4/spreadsheets")
    response = httpx.Response(
        400,
        json={"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "Invalid range name"}},
        request=request,
    )
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        GoogleSheetsClient("token")._raise_for_status(response, "edit Google Sheet")
    detail = GoogleSheetsAgent._http_status_error_detail(exc_info.value)
    assert "Invalid range name" in detail
    assert "INVALID_ARGUMENT" in detail


def test_cell_format_from_input_supports_rich_styles() -> None:
    cell_format, fields = GoogleSheetsAgent._cell_format_from_input(
        {
            "style": {
                "background_color": "#111827",
                "text_color": "#F9FAFB",
                "bold": True,
                "font_size": 14,
                "horizontal_alignment": "center",
                "vertical_alignment": "middle",
                "wrap_strategy": "wrap",
                "number_format_type": "currency",
                "number_format_pattern": "$#,##0.00",
            }
        }
    )

    assert cell_format["backgroundColor"] == {"red": 17 / 255, "green": 24 / 255, "blue": 39 / 255}
    assert cell_format["textFormat"]["foregroundColor"] == {"red": 249 / 255, "green": 250 / 255, "blue": 251 / 255}
    assert cell_format["textFormat"]["bold"] is True
    assert cell_format["textFormat"]["fontSize"] == 14
    assert cell_format["horizontalAlignment"] == "CENTER"
    assert cell_format["verticalAlignment"] == "MIDDLE"
    assert cell_format["wrapStrategy"] == "WRAP"
    assert cell_format["numberFormat"] == {"type": "CURRENCY", "pattern": "$#,##0.00"}
    assert fields == [
        "backgroundColor",
        "textFormat.foregroundColor",
        "textFormat.fontSize",
        "textFormat.bold",
        "horizontalAlignment",
        "verticalAlignment",
        "wrapStrategy",
        "numberFormat",
    ]


def test_border_sides_supports_outer_and_inner_aliases() -> None:
    assert GoogleSheetsAgent._border_sides("outer inner") == [
        "top",
        "bottom",
        "left",
        "right",
        "innerHorizontal",
        "innerVertical",
    ]


def test_format_range_builds_google_repeat_cell_request() -> None:
    class FakeClient:
        async def batch_update(self, spreadsheet_id, requests):
            return {"spreadsheet_id": spreadsheet_id, "requests": requests}

    agent = object.__new__(GoogleSheetsAgent)
    navigator = SheetNavigator(_sample_spreadsheet())
    result = asyncio.run(
        agent._format_range(
            FakeClient(),
            "sheet_123",
            navigator,
            range_name="'Pipeline'!A1:C1",
            input_data={"background_color": "#FF6600", "text_color": "#FFFFFF", "bold": True},
        )
    )

    repeat_cell = result["requests"][0]["repeatCell"]
    assert repeat_cell["range"] == {
        "sheetId": 111,
        "startRowIndex": 0,
        "endRowIndex": 1,
        "startColumnIndex": 0,
        "endColumnIndex": 3,
    }
    assert repeat_cell["cell"]["userEnteredFormat"]["backgroundColor"] == {"red": 1.0, "green": 102 / 255, "blue": 0.0}
    assert repeat_cell["cell"]["userEnteredFormat"]["textFormat"]["bold"] is True
    assert repeat_cell["fields"] == "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.foregroundColor,userEnteredFormat.textFormat.bold"
