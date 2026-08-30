import sqlite3, json, sys

rid = sys.argv[1]
con = sqlite3.connect("file:/home/ubuntu/Cosmic-OS/Backend/gateway/sessions.db?mode=ro", uri=True)
row = con.execute(
    "SELECT metadata_json FROM messages WHERE request_id=? AND role='assistant'",
    (rid,),
).fetchone()
m = json.loads(row[0])
for key in ("metrics", "activity_log"):
    v = m.get(key)
    print("#" * 30, key, "#" * 30)
    print(json.dumps(v, indent=1)[:6000] if v is not None else None)

tt = m.get("thinking_text") or ""
print("#" * 30, "thinking_text: len", len(tt), "#" * 30)
print(tt[:12000])
