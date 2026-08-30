SELECT session_id, channel, role, created_at, substr(replace(content, char(10), ' | '), 1, 400) AS c
FROM messages
WHERE session_id = 'sess_20260827'
ORDER BY created_at;
