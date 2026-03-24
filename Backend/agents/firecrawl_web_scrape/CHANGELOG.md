# Changelog

## 1.1.0 - 2026-03-23
- Added `firecrawl.agent` intent for autonomous AI-driven web extraction via Firecrawl's `/v2/agent` endpoint.
- Agent mode is a fallback for when simpler scrape/extract modes fail or are insufficient.
- Bumped `max_task_duration_sec` to 300 to accommodate longer agent jobs.

## 1.0.0 - 2026-03-16
- Initial Firecrawl Web Scrape Agent.
- Supports `firecrawl.scrape`, `firecrawl.extract`, and `firecrawl.recall_session`.
- Persists raw provider payloads and normalized artifacts under `runs/artifacts/<task_id>/`.
