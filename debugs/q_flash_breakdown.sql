SELECT llm_call_placed_at, prompt_tokens, completion_tokens, cached_tokens,
  json_extract(metadata_json, '$.iteration') AS iter,
  json_extract(metadata_json, '$.source') AS src,
  json_extract(metadata_json, '$.raw_usage.prompt_tokens') AS raw_prompt
FROM usage_events
WHERE model = 'accounts/fireworks/models/glm-5p3-flash'
ORDER BY llm_call_placed_at;
