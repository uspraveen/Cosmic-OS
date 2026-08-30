import sqlite3, json

con = sqlite3.connect("file:/home/ubuntu/Cosmic-OS/Backend/gateway/request_traces.db?mode=ro", uri=True)
row = con.execute(
    "SELECT events_json FROM request_traces WHERE request_id=?",
    ("req_a421b5bc-0b2b-4163-ab19-1ef0ba4649e2",),
).fetchone()
events = json.loads(row[0])
print("n_events:", len(events))
for e in events:
    et = e.get("event_type", "?")
    if et == "delivery.response.chunk":
        continue
    detail = str(e.get("detail") or "")[:400]
    print(f"{e.get('at','')[11:23]} | {et:36} | {str(e.get('title') or '')[:44]:44} | {detail if (detail:=str(e.get('detail') or ''))[:200] else ''}")
    md = e.get("metadata") or {}
    if md:
        print("    meta:", json.dumps(md)[:400])
