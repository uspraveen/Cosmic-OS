SELECT session_id, channel, created_at, substr(replace(content, char(10), ' | '), 1, 250) AS c
FROM messages
WHERE lower(content) LIKE '%parag%'
ORDER BY created_at;
