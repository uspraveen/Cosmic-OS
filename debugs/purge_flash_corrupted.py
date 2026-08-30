import sqlite3

DB = "/home/ubuntu/Cosmic-OS/Backend/gateway/usage.db"
MODEL = "accounts/fireworks/models/glm-5p3-flash"
CONTEXT_LIMIT = 1_040_000

con = sqlite3.connect(DB)
cur = con.cursor()
count, phantom = cur.execute(
    f"SELECT COUNT(*), ROUND(SUM(estimated_cost_usd), 2) FROM usage_events "
    f"WHERE model = ? AND prompt_tokens > ?",
    (MODEL, CONTEXT_LIMIT),
).fetchone()
print("corrupted rows:", count, "| phantom cost:", phantom)

cur.execute(
    f"DELETE FROM usage_events WHERE model = ? AND prompt_tokens > ?",
    (MODEL, CONTEXT_LIMIT),
)
con.commit()
print("deleted:", cur.rowcount)

total = cur.execute(
    f"SELECT COUNT(*), SUM(prompt_tokens), SUM(completion_tokens), ROUND(SUM(estimated_cost_usd), 4) "
    f"FROM usage_events WHERE model = ?",
    (MODEL,),
).fetchone()
print("remaining:", total[0], "events | prompt:", total[1], "| completion:", total[2], "| cost:", total[3])
con.close()
