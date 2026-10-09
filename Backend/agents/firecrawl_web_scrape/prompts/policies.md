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
- Prefer `firecrawl.search` when no URL is known yet or the orchestrator needs news/image results or vertical (developer/research/gov/pdf) coverage. Adding `sources: ["alexandria"]` lists matching catalogue tools for free in the same call.
- Alexandria tools are executed contract-first with `firecrawl.alexandria`: send the provider/capability ids from discovery and exactly the options the contract declares (`tool_detail: "full"` on discovery returns the contract). Never invent a provider, capability, or option name.
- Check every Alexandria result's `provider_error` and `success` fields — the HTTP call succeeding does not mean the tool result succeeded. An `invalid_option` error lists the valid options: fix the options and retry once. `THIRD_PARTY_DATA_TERMS_REQUIRED` carries a `requires_action_url`: surface it to the orchestrator for the user and do not retry — the call cannot accept terms on the user's behalf.
- Track Alexandria spend in every output (`credits_cost`, `task_credits_spent`, `task_credit_cap`, `budget_exceeded`). When the per-task cap is reached (`budget_exceeded` or a `BUDGET_EXCEEDED` error), stop calling tools and return what was gathered.
- For image-locked data (tables/charts rendered as pictures), read it visually: scrape the direct image URL if known (it is fetched as an image artifact), or request `formats: ["screenshot"]` (captured full-page by default). Both are persisted as image artifacts for the orchestrator's vision model.
- For PDF or scanned-document sources, pass `parsers` (e.g. `["pdf"]`) to force OCR/parsing.
- Persist the raw provider response to artifacts for every successful scrape, search, or extract run.
- Do not emit excessively noisy progress events; use milestone progress only.

## Data Integrity
- Extraction must never fabricate, guess, infer, or approximate values. Absent fields are returned as `null`.
- Alexandria records are typed and sourced by the provider; pass values through untouched, report `records_count`, and treat a per-result error entry as "no data" — never as data to interpret.
- Inline excerpts are bounded for size; full bodies always live in artifacts. When an excerpt is truncated, the output flags it and names the full artifact.
- A screenshot artifact is the supported path for reading numbers that exist only as an image; do not invent those numbers from partial text.

## Storage Rules
- Store only compact per-task summaries in `store/data/`.
- Keep full scraped and extracted bodies in artifacts.
- Do not write ephemeral chatter or partial polling traces into shared memory.
