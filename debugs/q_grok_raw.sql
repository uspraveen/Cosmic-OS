SELECT llm_call_id, model, prompt_tokens, completion_tokens, cached_tokens, estimated_cost_usd,
  substr(metadata_json, 1, 600) AS meta
FROM usage_events
WHERE model = 'grok-4-1-fast-reasoning' AND llm_call_placed_at >= '2026-08-28'
ORDER BY llm_call_placed_at DESC
LIMIT 2;
