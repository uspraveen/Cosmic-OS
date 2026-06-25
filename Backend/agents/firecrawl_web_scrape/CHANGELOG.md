# Changelog

## 1.3.0 - 2026-06-24
- Screenshots are now captured full-page by default (via Firecrawl's `{"type":"screenshot","fullPage":true}`), so image-based tables/charts below the fold are included rather than just the top viewport. Override with `screenshot_full_page: false`.
- `firecrawl.scrape` now handles direct image URLs (e.g. a `.png` chart/table): instead of failing on Firecrawl's "cannot process binary image" error, the agent downloads the image and surfaces it as a vision-readable artifact. Also auto-falls back to direct image fetch when Firecrawl rejects a URL as an image content-type.

## 1.2.0 - 2026-06-24
- Screenshots captured via `formats:["screenshot"]` are now persisted as real `image/png` artifacts with a fetchable URL, so the orchestrator's existing tool-result image pipeline surfaces them to the vision model (Kimi) for reading image-locked tables/charts. The default text orchestrator cannot read images; COSMIC auto-escalates to the vision model when an image artifact is surfaced.
- Added `parsers` passthrough to `firecrawl.scrape` and `firecrawl.extract` for Firecrawl OCR/parsing of PDF and scanned-document sources.
- Inline markdown/HTML excerpt budgets are now config-driven (`FIRECRAWL_INLINE_MARKDOWN_CHARS` / `FIRECRAWL_INLINE_HTML_CHARS`, defaults raised to 12000/6000) and truncation is flagged with a pointer to the full artifact.
- `firecrawl.extract` now instructs Firecrawl to return null for absent fields and never fabricate values.

## 1.1.0 - 2026-03-23
- Added `firecrawl.agent` intent for autonomous AI-driven web extraction via Firecrawl's `/v2/agent` endpoint.
- Agent mode is a fallback for when simpler scrape/extract modes fail or are insufficient.
- Bumped `max_task_duration_sec` to 300 to accommodate longer agent jobs.

## 1.0.0 - 2026-03-16
- Initial Firecrawl Web Scrape Agent.
- Supports `firecrawl.scrape`, `firecrawl.extract`, and `firecrawl.recall_session`.
- Persists raw provider payloads and normalized artifacts under `runs/artifacts/<task_id>/`.
