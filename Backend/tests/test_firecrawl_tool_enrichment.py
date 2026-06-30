from __future__ import annotations

from orchestrator.firecrawl_tool_enrichment import enrich_firecrawl_tool_result


def test_enrich_firecrawl_tool_result_adds_guidance_for_truncated_html() -> None:
    result = {
        "url": "https://example.com",
        "data": {
            "raw_html_excerpt": "<html>...</html>",
            "raw_html_truncated": True,
            "raw_html_full_artifact": "page.raw.html",
        },
        "artifacts": [
            {
                "artifact_id": "art_abc123",
                "path": "runs/artifacts/tsk_1/firecrawl_web_scrape/page.raw.html",
                "filename": "page.raw.html",
                "audience": "supporting",
            }
        ],
    }

    enriched = enrich_firecrawl_tool_result(result)

    assert enriched["truncation_detected"] is True
    assert "artifact_read" in enriched["full_content_guidance"]
    assert "art_abc123" in enriched["full_content_guidance"]
    assert "firecrawl_recall_session" in enriched["full_content_guidance"]


def test_enrich_firecrawl_tool_result_noop_when_not_truncated() -> None:
    result = {
        "data": {"markdown_excerpt": "short"},
        "artifacts": [],
    }
    assert enrich_firecrawl_tool_result(result) == result
