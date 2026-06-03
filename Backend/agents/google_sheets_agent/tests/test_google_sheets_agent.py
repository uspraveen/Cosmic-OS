"""Focused tests for the Google Sheets specialist agent."""

from __future__ import annotations

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

