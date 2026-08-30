SELECT request_id, session_id, channel, route, status, user_query_excerpt, created_at
FROM request_traces
WHERE session_id = 'sess_20260828'
  AND created_at >= '2026-08-29T05:45'
ORDER BY created_at;
