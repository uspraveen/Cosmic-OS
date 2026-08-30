import sqlite3

con = sqlite3.connect("file:/home/ubuntu/Cosmic-OS/Backend/gateway/usage.db?mode=ro", uri=True)
rows = con.execute(
    "SELECT model, json_extract(metadata_json,'$.preferred_model'),"
    " json_extract(metadata_json,'$.reasoning_effort'), llm_call_placed_at"
    " FROM usage_events WHERE operation='orchestrator.process'"
    " ORDER BY llm_call_placed_at DESC LIMIT 5"
).fetchall()
for r in rows:
    print(r)
