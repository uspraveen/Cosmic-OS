SELECT llm_call_id, llm_call_placed_at, source_id, prompt_tokens, completion_tokens, cached_tokens,
  ROUND(estimated_cost_usd, 4) AS cost, llm_call_placed_at < '2026-08-29T19:10:00' AS before_fix
FROM usage_events
WHERE model = 'accounts/fireworks/models/glm-5p3-flash'
ORDER BY llm_call_placed_at;
