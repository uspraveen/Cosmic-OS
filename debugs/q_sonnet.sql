SELECT llm_call_id, llm_call_placed_at, source_component, operation, success, error_code,
  prompt_tokens, completion_tokens, estimated_cost_usd
FROM usage_events
WHERE model = 'claude-sonnet-4-6' AND prompt_tokens > 1040000
ORDER BY llm_call_placed_at;
