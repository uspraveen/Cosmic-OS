SELECT llm_call_id, model, prompt_tokens, completion_tokens, cached_tokens, estimated_cost_usd,
  substr(metadata_json, 1, 700) AS meta
FROM usage_events
WHERE model LIKE '%glm-5p3-flash%'
ORDER BY llm_call_placed_at DESC
LIMIT 3;
