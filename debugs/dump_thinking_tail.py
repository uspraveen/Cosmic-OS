import sqlite3, json, sys

rid = sys.argv[1]
con = sqlite3.connect("file:/home/ubuntu/Cosmic-OS/Backend/gateway/sessions.db?mode=ro", uri=True)
row = con.execute("SELECT metadata_json FROM messages WHERE request_id=? AND role='assistant'", (rid,)).fetchone()
m = json.loads(row[0])
tt = m.get("thinking_text") or ""
print("len:", len(tt))
print("----- LAST 3500 chars -----")
print(tt[-3500:])

# Also compare turn 1
print()
row1 = con.execute("SELECT metadata_json, content FROM messages WHERE request_id=? AND role='assistant'", ("req_8e2bb53c-4945-4610-9101-6604d2284795",)).fetchone()
m1 = json.loads(row1[0])
print("TURN1 metrics:", json.dumps(m1.get("metrics"), indent=1))
print("TURN1 thinking len:", len(m1.get("thinking_text") or ""))
