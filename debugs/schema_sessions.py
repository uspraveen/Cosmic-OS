import sqlite3, json, sys

con = sqlite3.connect("file:/home/ubuntu/Cosmic-OS/Backend/gateway/sessions.db?mode=ro", uri=True)
tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print("TABLES:", tables)
for t in tables:
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
    n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"  {t} ({n} rows): {cols}")
