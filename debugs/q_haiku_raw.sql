SELECT llm_call_id, operation, success, error_code, prompt_tokens, completion_tokens,
  substr(metadata_json, 1, 500) AS meta
FROM usage_events
WHERE provider = 'anthropic' AND llm_call_placed_at >= '2026-08-28'
ORDER BY llm_call_placed_at DESC
LIMIT 4;
