SELECT session_id, channel, role, created_at, substr(replace(content, char(10), ' | '), 1, 300) AS c
FROM messages
WHERE created_at >= '2026-08-28T20:00'
  AND channel LIKE 'desktop%'
ORDER BY created_at;
