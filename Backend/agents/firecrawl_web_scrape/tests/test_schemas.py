from __future__ import annotations

import json
from pathlib import Path


SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "schemas" / "intents"


def test_firecrawl_intent_schemas_parse_as_json_and_proxy_enum_matches_runtime() -> None:
    schema_names = [
        "firecrawl.scrape.input.json",
        "firecrawl.scrape.output.json",
        "firecrawl.extract.input.json",
        "firecrawl.extract.output.json",
        "firecrawl.recall_session.input.json",
        "firecrawl.recall_session.output.json",
    ]
    for name in schema_names:
        payload = json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))
        assert payload["type"] == "object"

    scrape_input = json.loads((SCHEMA_ROOT / "firecrawl.scrape.input.json").read_text(encoding="utf-8"))
    assert scrape_input["properties"]["proxy"]["enum"] == ["auto", "basic", "enhanced"]
