#!/usr/bin/env python3
"""Ad-hoc sqlite inspector for Cosmic VM debugging."""
import sqlite3, sys, json, os

path = sys.argv[1]
mode = sys.argv[2] if len(sys.argv) > 2 else "tables"

con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
cur = con.cursor()

def show(rows):
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if isinstance(v, str) and len(v) > 600:
                d[k] = v[:600] + f"...[+{len(v)-600} chars]"
        print(json.dumps(d, default=str, ensure_ascii=False))

if mode == "tables":
    for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        print(r["name"])
elif mode == "schema":
    for r in cur.execute("SELECT name, sql FROM sqlite_master WHERE type='table'"):
        print(r["sql"]); print("---")
elif mode == "query":
    sql = sys.argv[3]
    params = sys.argv[4:] if len(sys.argv) > 4 else []
    rows = cur.execute(sql, params).fetchall()
    print(f"[{len(rows)} rows]")
    show(rows[:50])
elif mode == "sqlfile":
    with open(sys.argv[3], "r", encoding="utf-8") as f:
        sql = f.read()
    rows = cur.execute(sql).fetchall()
    print(f"[{len(rows)} rows]")
    show(rows[:200])
