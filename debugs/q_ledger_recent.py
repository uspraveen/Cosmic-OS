import sqlite3

con = sqlite3.connect("file:/home/ubuntu/Cosmic-OS/Backend/agents/orchestrator/store/data/task_ledger.db?mode=ro", uri=True)
rows = con.execute(
    "SELECT task_id, status, error_code, error_message, created_at FROM tasks "
    "WHERE created_at >= '2026-08-30T19:50' ORDER BY created_at LIMIT 10"
).fetchall()
cols = [r[1] for r in con.execute("PRAGMA table_info(tasks)")]
print("cols:", cols)
for r in rows:
    print(r)
