import sqlite3, json, sys

path = sys.argv[1]
request_id = sys.argv[2]

con = sqlite3.connect(path)
con.row_factory = sqlite3.Row
row = con.execute(
    "SELECT events_json, delivery_json FROM request_traces WHERE request_id=?", (request_id,)
).fetchone()
if row is None:
    print("NO TRACE")
    sys.exit(0)

ev = json.loads(row["events_json"])
if isinstance(ev, dict):
    ev = [ev]

for e in ev:
    et = e.get("type") or e.get("event_type")
    s = json.dumps(e)
    marker = "MEMORY" if "memor" in s.lower() else ""
    print("EVENT", et, marker, "len=", len(s))
    if marker:
        print(s[:12000])
        print()

# delivery too
d = row["delivery_json"]
if d:
    dj = json.loads(d)
    s = json.dumps(dj)
    if "memor" in s.lower():
        print("DELIVERY", s[:4000])
