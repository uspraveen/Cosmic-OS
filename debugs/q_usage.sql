SELECT provider, model, COUNT(*) AS calls,
  SUM(prompt_tokens) AS prompt, SUM(cached_tokens) AS cached,
  SUM(reasoning_tokens) AS reasoning, SUM(completion_tokens) AS completion,
  ROUND(SUM(estimated_cost_usd), 4) AS cost_usd
FROM usage_events
WHERE llm_call_placed_at >= '2026-08-28'
GROUP BY provider, model
ORDER BY cost_usd DESC
LIMIT 12;
