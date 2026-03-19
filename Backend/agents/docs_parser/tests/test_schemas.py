from __future__ import annotations

import json
from pathlib import Path


SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "schemas" / "intents"


def test_docs_parser_intent_schemas_parse_as_json_and_ocr_enum_matches_runtime() -> None:
    schema_names = [
        "docs.parse_bundle.input.json",
        "docs.parse_bundle.output.json",
        "docs.search_bundle.input.json",
        "docs.search_bundle.output.json",
        "docs.read_bundle.input.json",
        "docs.read_bundle.output.json",
    ]
    for name in schema_names:
        payload = json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))
        assert payload["type"] == "object"

    parse_input = json.loads((SCHEMA_ROOT / "docs.parse_bundle.input.json").read_text(encoding="utf-8"))
    assert parse_input["properties"]["ocr_mode"]["enum"] == ["auto", "off", "force"]
