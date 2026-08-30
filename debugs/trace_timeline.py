import sqlite3, json, sys
from datetime import datetime

DB = "file:/home/ubuntu/Cosmic-OS/Backend/gateway/request_traces.db?mode=ro"
rid = sys.argv[1]
full = len(sys.argv) > 2 and sys.argv[2] == "full"

con = sqlite3.connect(DB, uri=True)
row = con.execute("SELECT events_json, created_at, completed_at FROM request_traces WHERE request_id=?", (rid,)).fetchone()
events = json.loads(row[0])
t0 = datetime.fromisoformat(row[1].replace("Z", "+00:00"))
t1 = datetime.fromisoformat(row[2].replace("Z", "+00:00"))
print("REQ", rid, "created:", row[1], "completed:", row[2], "TOTAL: %.1fs" % (t1 - t0).total_seconds())
print("n_events:", len(events))
print("=" * 110)

prev = t0
for e in events:
    at = datetime.fromisoformat(e["at"].replace("Z", "+00:00"))
    dt = (at - prev).total_seconds()
    prev = at
    off = (at - t0).total_seconds()
    meta = e.get("metadata") or {}
    meta_keys = {k: v for k, v in meta.items() if k in ("iteration", "source", "model", "provider", "prompt_tokens", "completion_tokens", "cached_tokens", "latency_ms", "duration_ms", "tool", "agent", "route", "ttft_ms", "thinking", "reasoning", "stream")}
    print(f"{off:8.1f}s +{dt:6.1f}s | {e.get('event_type','?'):30} | {(e.get('title') or '')[:55]:55} | {str(e.get('detail') or '')[:90]}")
    if meta_keys:
        print(f"{'':18} meta: {json.dumps(meta_keys)[:300]}")
    if full and e.get("detail"):
        d = str(e.get("detail"))
        if len(d) > 90:
            print(f"{'':18} detail_full: {d[:2000]}")
