import sqlite3, sys
from datetime import datetime

con = sqlite3.connect("file:/home/ubuntu/Cosmic-OS/Backend/gateway/usage.db?mode=ro", uri=True)
cols = [r[1] for r in con.execute("PRAGMA table_info(usage_events)")]
print("COLS:", cols)
lo, hi = sys.argv[1], sys.argv[2]
rows = con.execute("SELECT * FROM usage_events WHERE llm_call_placed_at BETWEEN ? AND ? ORDER BY llm_call_placed_at", (lo, hi)).fetchall()
print("rows:", len(rows))
for r in rows:
    d = dict(zip(cols, r))
    print("-" * 100)
    for k in cols:
        v = d.get(k)
        if v is None:
            continue
        s = str(v)
        if len(s) > 200:
            s = s[:200] + "..."
        print(f"  {k}: {s}")
