import sqlite3

DB = "/home/ubuntu/Cosmic-OS/Backend/gateway/usage.db"

con = sqlite3.connect(DB)
cur = con.cursor()
cur.execute(
    "DELETE FROM usage_events WHERE model = 'claude-sonnet-4-6' AND prompt_tokens > 1040000"
)
con.commit()
print("deleted sonnet rows:", cur.rowcount)
left = cur.execute(
    "SELECT COUNT(*) FROM usage_events WHERE prompt_tokens > 1040000"
).fetchone()
print("remaining impossible rows anywhere:", left[0])
con.close()
