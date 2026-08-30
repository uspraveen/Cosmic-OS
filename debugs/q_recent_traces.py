import sqlite3

con = sqlite3.connect("file:/home/ubuntu/Cosmic-OS/Backend/gateway/request_traces.db?mode=ro", uri=True)
rows = con.execute(
    "SELECT request_id, created_at, status, final_event_type, final_message, "
    "json_extract(events_json,'$[0].at') FROM request_traces "
    "WHERE created_at >= '2026-08-30T19:54' ORDER BY created_at"
).fetchall()
for r in rows:
    print(r[3], "|", r[1], "->", r[2], "|", r[0])
    if r[1] and "NoneType" in str(r[1]):
        print("   !!", str(r[3])[:300])

print("---- final_message / final_event for recent requests ----")
rows2 = con.execute(
    "SELECT request_id, status, final_event_type, final_message, created_at FROM request_traces "
    "WHERE created_at >= '2026-08-30T19:20' ORDER BY created_at"
).fetchall()
for r in rows2:
    print(r[0][:44], "|", r[2], "|", str(r[3])[:200])
