SELECT provider, model, COUNT(*) AS n, ROUND(SUM(estimated_cost_usd), 2) AS cost
FROM usage_events
WHERE prompt_tokens > 1040000
GROUP BY provider, model;
