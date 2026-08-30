import sqlite3, json
from collections import Counter

con = sqlite3.connect("file:/home/ubuntu/Cosmic-OS/Backend/gateway/request_traces.db?mode=ro", uri=True)
for rid in ("req_8e2bb53c-4945-4610-9101-6604d2284795", "req_96aea6ab-4e50-44a7-8b12-2cc56659d3ef"):
    ej = con.execute("SELECT events_json FROM request_traces WHERE request_id=?", (rid,)).fetchone()[0]
    events = json.loads(ej)
    c = Counter(e.get("event_type") for e in events)
    ats = [e["at"] for e in events]
    print(rid, "n=", len(events), "first_at:", min(ats), "last_at:", max(ats))
    print("  ", dict(c.most_common(10)))
