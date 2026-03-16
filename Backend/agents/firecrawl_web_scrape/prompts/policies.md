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
- Persist the raw provider response to artifacts for every successful scrape or extract run.
- Do not emit excessively noisy progress events; use milestone progress only.

## Storage Rules
- Store only compact per-task summaries in `store/data/`.
- Keep full scraped and extracted bodies in artifacts.
- Do not write ephemeral chatter or partial polling traces into shared memory.
