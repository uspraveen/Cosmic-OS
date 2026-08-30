SELECT operation, success, error_code, COUNT(*) AS n, MIN(llm_call_placed_at) AS first, MAX(llm_call_placed_at) AS last
FROM usage_events
WHERE provider = 'anthropic' AND llm_call_placed_at >= '2026-08-28'
GROUP BY operation, success, error_code
ORDER BY n DESC
LIMIT 10;
