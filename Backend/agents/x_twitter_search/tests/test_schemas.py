from __future__ import annotations

import json
from pathlib import Path


SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "schemas" / "intents"


def test_x_search_intent_schemas_parse_as_json() -> None:
    schema_names = [
        "x.search.input.json",
        "x.search.output.json",
        "x.recall_session.input.json",
        "x.recall_session.output.json",
    ]
    for name in schema_names:
        payload = json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))
        assert payload["type"] == "object"

    search_input = json.loads((SCHEMA_ROOT / "x.search.input.json").read_text(encoding="utf-8"))
    assert search_input["properties"]["max_posts"]["maximum"] == 30
