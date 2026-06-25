# Policies

## Error Handling
- Return `TIMEOUT` with `retryable=true` when Firecrawl or polling exceeds the configured timeout.
- Return `NETWORK_ERROR` with `retryable=true` for transport failures and upstream 5xx errors.
- Return `RATE_LIMITED` with `retryable=true` for Firecrawl 429 responses.
- Return `AUTH_ERROR` with `retryable=false` when the Firecrawl API key is missing or rejected.
- Return `INVALID_INPUT` with `retryable=false` for malformed URLs, empty prompts, unsupported formats, or invalid schemas.
- Return `INTERNAL_ERROR` with `retryable=false` for unexpected provider payloads or local persistence failures.

## Tool Usage
- Prefer `firecrawl.scrape` for one URL when the orchestrator needs clean content or page metadata.
- Prefer `firecrawl.extract` when the orchestrator needs structured fields across one or more URLs.
- For image-locked data (tables/charts rendered as pictures), read it visually: scrape the direct image URL if known (it is fetched as an image artifact), or request `formats: ["screenshot"]` (captured full-page by default). Both are persisted as image artifacts for the orchestrator's vision model.
- For PDF or scanned-document sources, pass `parsers` (e.g. `["pdf"]`) to force OCR/parsing.
- Persist the raw provider response to artifacts for every successful scrape or extract run.
- Do not emit excessively noisy progress events; use milestone progress only.

## Data Integrity
- Extraction must never fabricate, guess, infer, or approximate values. Absent fields are returned as `null`.
- Inline excerpts are bounded for size; full bodies always live in artifacts. When an excerpt is truncated, the output flags it and names the full artifact.
- A screenshot artifact is the supported path for reading numbers that exist only as an image; do not invent those numbers from partial text.

## Storage Rules
- Store only compact per-task summaries in `store/data/`.
- Keep full scraped and extracted bodies in artifacts.
- Do not write ephemeral chatter or partial polling traces into shared memory.
