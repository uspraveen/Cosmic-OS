SELECT request_id, session_id, state, memory_id, last_error, created_at, updated_at
FROM memory_episode_links
WHERE session_id LIKE 'email-thread:iamcosmic001%ead1af0b%'
ORDER BY created_at;
