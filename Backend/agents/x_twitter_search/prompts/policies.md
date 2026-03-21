# Policies

## Error Handling
- Return `TIMEOUT` with `retryable=true` when xAI or local waiting exceeds the configured timeout.
- Return `NETWORK_ERROR` with `retryable=true` for transport failures and upstream 5xx errors.
- Return `RATE_LIMITED` with `retryable=true` for xAI 429 responses.
- Return `AUTH_ERROR` with `retryable=false` when the xAI API key is missing or rejected.
- Return `INVALID_INPUT` with `retryable=false` for malformed filters, invalid date ranges, or empty queries.
- Return `INTERNAL_ERROR` with `retryable=false` for unexpected provider payloads or local persistence failures.

## Search Quality
- Search X first; do not drift into generic web synthesis.
- Prefer a compact, evidence-grounded briefing over a vague narrative.
- Capture notable posts only when they materially support the answer.
- Do not invent handles, timestamps, or post URLs if the provider response does not support them.

## Storage Rules
- Persist the raw provider response and a normalized report for every successful search.
- Store only compact per-task summaries in `store/data/`.
- Keep full raw bodies in artifacts.
- Do not write ephemeral chatter into shared memory.

