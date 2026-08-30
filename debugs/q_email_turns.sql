SELECT request_id, session_id, channel, route, started_at, completed_at, user_message_excerpt, compact_line
FROM turn_ledger
WHERE session_id LIKE 'email-thread:iamcosmic001%'
ORDER BY started_at DESC
LIMIT 15;
