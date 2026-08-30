import sqlite3, json, sys

rid = sys.argv[1]
con = sqlite3.connect("file:/home/ubuntu/Cosmic-OS/Backend/gateway/sessions.db?mode=ro", uri=True)
rows = con.execute(
    "SELECT message_id, role, created_at, content, metadata_json, route FROM messages WHERE request_id=? ORDER BY created_at",
    (rid,),
).fetchall()
print("messages for", rid, ":", len(rows))
for mid, role, cat, content, meta, route in rows:
    m = json.loads(meta or "{}")
    print("=" * 100)
    print(mid, "|", role, "|", cat, "| route:", route)
    print("meta keys:", sorted(m.keys()))
    c = content or ""
    print("content len:", len(c))
    # Show thinking-ish fields in metadata
    for k in ("thinking", "reasoning", "tool_calls", "tool_results", "iterations", "model", "usage", "specialist_receipts", "blocks", "streamed"):
        if k in m:
            v = m[k]
            s = json.dumps(v)
            print(f"  meta.{k}: {s[:1500]}")
    print("content head:", c[:600].replace("\n", " | "))
    print("content tail:", c[-400:].replace("\n", " | "))
