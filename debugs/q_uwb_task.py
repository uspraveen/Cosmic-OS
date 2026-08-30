import sqlite3, json

con = sqlite3.connect(
    "file:/home/ubuntu/Cosmic-OS/Backend/agents/orchestrator/store/data/task_ledger.db?mode=ro",
    uri=True,
)
row = con.execute(
    "SELECT task_id, query, status, result_json FROM tasks WHERE task_id=?",
    ("tsk_d9362b440aaa",),
).fetchone()
print("task:", row[0], "| status:", row[2])
print("query:", row[1][:120])
r = json.loads(row[3] or "{}")
print("result_type:", r.get("result_type"), "| stop_reason:", r.get("stop_reason"))
print("loop_diagnostics:", json.dumps((r.get("loop_diagnostics") or {}), indent=1))
print("usage:", json.dumps(r.get("usage") or {}))
print("content head:", (r.get("content") or "")[:400])
print("thinking len:", len(r.get("thinking_text") or ""))
